import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord.ext import commands, tasks

CAMINHO_BANCO = Path(__file__).resolve().parents[1] / "config" / "investimentos.sqlite3"
SALDO_INICIAL_CENTAVOS = 1_000_000
ATIVOS = {
    "LFTS11": ("LFTS11.SA", "Tesouro Selic ETF"),
    "BOVA11": ("BOVA11.SA", "ETF de ações brasileiras"),
    "IVVB11": ("IVVB11.SA", "ETF de ações dos EUA"),
}
PERFIS = {
    "conservador": {"TESOURO_SELIC": 100},
    "moderado": {"TESOURO_SELIC": 50, "BOVA11": 20, "IVVB11": 30},
    "arrojado": {"LFTS11": 20, "BOVA11": 30, "IVVB11": 50},
}


def formatar_reais(valor: Decimal) -> str:
    formatado = f"{valor:,.2f}"
    return "R$ " + formatado.replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_percentual(valor: Decimal) -> str:
    return f"{valor:.2f}%".replace(".", ",")


def para_decimal(texto: str) -> Decimal | None:
    try:
        valor = Decimal(texto.strip().replace("R$", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    return valor if valor.is_finite() and valor > 0 else None


def conectar_banco() -> sqlite3.Connection:
    CAMINHO_BANCO.parent.mkdir(parents=True, exist_ok=True)
    banco = sqlite3.connect(CAMINHO_BANCO, timeout=10)
    banco.execute(
        """
        CREATE TABLE IF NOT EXISTS investimento_demo_contas (
            usuario_id INTEGER PRIMARY KEY,
            saldo_centavos INTEGER NOT NULL
        )
        """
    )
    banco.execute(
        """
        CREATE TABLE IF NOT EXISTS investimento_demo_posicoes (
            usuario_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            quantidade INTEGER NOT NULL,
            custo_centavos INTEGER NOT NULL,
            PRIMARY KEY (usuario_id, ticker)
        )
        """
    )
    banco.execute(
        """
        CREATE TABLE IF NOT EXISTS investimento_demo_alertas (
            usuario_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            limite_percentual TEXT NOT NULL,
            preco_referencia TEXT NOT NULL,
            alerta_ativa INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (usuario_id, ticker)
        )
        """
    )
    return banco


def obter_conta(banco: sqlite3.Connection, usuario_id: int) -> int:
    linha = banco.execute(
        "SELECT saldo_centavos FROM investimento_demo_contas WHERE usuario_id = ?",
        (usuario_id,),
    ).fetchone()
    if linha is None:
        banco.execute(
            "INSERT INTO investimento_demo_contas (usuario_id, saldo_centavos) VALUES (?, ?)",
            (usuario_id, SALDO_INICIAL_CENTAVOS),
        )
        return SALDO_INICIAL_CENTAVOS
    return int(linha[0])


def consultar_cotacao_sync(ticker: str) -> dict[str, object]:
    simbolo, nome = ATIVOS[ticker]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}?range=3mo&interval=1d"
    requisicao = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(requisicao, timeout=12) as resposta:
            dados = json.load(resposta)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as erro:
        raise RuntimeError("não foi possível consultar a fonte de cotações agora") from erro

    resultados = dados.get("chart", {}).get("result") or []
    if not resultados:
        raise RuntimeError(f"a fonte não retornou cotação para {ticker}")
    resultado = resultados[0]
    metadados = resultado.get("meta", {})
    fechamentos = resultado.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    valores_validos = [Decimal(str(valor)) for valor in fechamentos if valor is not None]
    preco_atual = metadados.get("regularMarketPrice")
    if preco_atual is None and valores_validos:
        preco_atual = valores_validos[-1]
    if preco_atual is None:
        raise RuntimeError(f"a fonte não retornou preço para {ticker}")

    preco = Decimal(str(preco_atual))
    variacao_mes = None
    if len(valores_validos) > 1 and valores_validos[-1] != 0:
        inicio_mes = valores_validos[max(0, len(valores_validos) - 22)]
        variacao_mes = (valores_validos[-1] / inicio_mes - 1) * 100
    horario = metadados.get("regularMarketTime")
    atualizado_em = (
        datetime.fromtimestamp(horario, tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        if horario
        else "horário indisponível"
    )
    return {
        "ticker": ticker,
        "nome": nome,
        "preco": preco,
        "variacao_mes": variacao_mes,
        "atualizado_em": atualizado_em,
    }


async def consultar_cotacao(ticker: str) -> dict[str, object]:
    return await asyncio.to_thread(consultar_cotacao_sync, ticker)


def consultar_tesouro_selic_sync() -> dict[str, object]:
    url = (
        "https://brapi.dev/api/v2/treasury/indicators"
        "?symbols=tesouro-selic-01032031"
    )
    requisicao = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(requisicao, timeout=12) as resposta:
            dados = json.load(resposta)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as erro:
        raise RuntimeError("não foi possível consultar a taxa do Tesouro Selic agora") from erro
    resultados = dados.get("results", [])
    if not resultados:
        raise RuntimeError("a fonte não retornou dados do Tesouro Selic")
    titulo = resultados[0]
    return {
        "preco": Decimal(str(titulo["buyPrice"])),
        "taxa_compra": Decimal(str(titulo["buyRate"])),
        "data_base": str(titulo["baseDate"]),
        "vencimento": str(titulo["maturityDate"]),
    }


async def consultar_tesouro_selic() -> dict[str, object]:
    return await asyncio.to_thread(consultar_tesouro_selic_sync)


def normalizar_ticker(ticker: str) -> str | None:
    ticker = ticker.upper().strip().removesuffix(".SA")
    return ticker if ticker in ATIVOS else None


def avaliar_queda(
    preco: Decimal,
    referencia: Decimal,
    limite_percentual: Decimal,
    alerta_ativa: bool,
) -> tuple[Decimal, bool, bool]:
    limite = referencia * (1 - limite_percentual / 100)
    if alerta_ativa:
        limite_recuperacao = referencia * (1 - limite_percentual / 200)
        if preco >= limite_recuperacao:
            return preco, False, False
        return referencia, True, False
    if preco > referencia:
        return preco, False, False
    if preco <= limite:
        return referencia, True, True
    return referencia, False, False


def registrar_venda_simulada(
    usuario_id: int,
    ticker: str,
    quantidade_solicitada: int | None,
    preco_centavos: int,
) -> tuple[int, int, int] | None:
    banco = conectar_banco()
    try:
        banco.execute("BEGIN IMMEDIATE")
        posicao = banco.execute(
            "SELECT quantidade, custo_centavos FROM investimento_demo_posicoes "
            "WHERE usuario_id = ? AND ticker = ?",
            (usuario_id, ticker),
        ).fetchone()
        if posicao is None:
            banco.rollback()
            return None

        quantidade_anterior, custo_anterior = int(posicao[0]), int(posicao[1])
        quantidade = quantidade_anterior if quantidade_solicitada is None else quantidade_solicitada
        if quantidade < 1 or quantidade > quantidade_anterior:
            banco.rollback()
            return None
        custo_removido = int(
            (Decimal(custo_anterior) * quantidade / quantidade_anterior).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        restante = quantidade_anterior - quantidade
        if restante:
            banco.execute(
                "UPDATE investimento_demo_posicoes SET quantidade = ?, custo_centavos = ? "
                "WHERE usuario_id = ? AND ticker = ?",
                (restante, custo_anterior - custo_removido, usuario_id, ticker),
            )
        else:
            banco.execute(
                "DELETE FROM investimento_demo_posicoes WHERE usuario_id = ? AND ticker = ?",
                (usuario_id, ticker),
            )
            banco.execute(
                "DELETE FROM investimento_demo_alertas WHERE usuario_id = ? AND ticker = ?",
                (usuario_id, ticker),
            )
        recebido = quantidade * preco_centavos
        banco.execute(
            "UPDATE investimento_demo_contas SET saldo_centavos = saldo_centavos + ? WHERE usuario_id = ?",
            (recebido, usuario_id),
        )
        banco.commit()
        return quantidade, recebido, recebido - custo_removido
    except Exception:
        banco.rollback()
        raise
    finally:
        banco.close()


class AlertaQuedaView(discord.ui.View):
    def __init__(self, usuario_id: int, ticker: str) -> None:
        super().__init__(timeout=86400)
        self.usuario_id = usuario_id
        self.ticker = ticker
        self.mensagem: discord.Message | None = None

    def desabilitar_botoes(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.usuario_id:
            await interaction.response.send_message(
                "Somente quem recebeu este alerta pode escolher uma ação.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Vender tudo (simulação)", style=discord.ButtonStyle.danger)
    async def vender_tudo(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        try:
            cotacao = await consultar_cotacao(self.ticker)
        except RuntimeError as erro:
            await interaction.followup.send(f"Não consegui atualizar a cotação: {erro}.")
            return

        preco_centavos = int(
            (cotacao["preco"] * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        venda = registrar_venda_simulada(
            self.usuario_id, self.ticker, None, preco_centavos
        )
        if venda is None:
            self.desabilitar_botoes()
            await interaction.followup.send(
                f"Você não possui mais cotas de {self.ticker} na carteira simulada."
            )
            await interaction.message.edit(view=self)
            return

        quantidade, recebido, resultado = venda
        self.desabilitar_botoes()
        await interaction.followup.send(
            f"Venda simulada concluída: **{quantidade} {self.ticker}** por "
            f"{formatar_reais(Decimal(recebido) / 100)}. Resultado realizado: "
            f"{formatar_reais(Decimal(resultado) / 100)}. Nenhuma ordem real foi enviada."
        )
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Manter ativo", style=discord.ButtonStyle.secondary)
    async def manter(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.desabilitar_botoes()
        await interaction.response.edit_message(
            content="Você escolheu manter o ativo na carteira simulada.", view=self
        )

    async def on_timeout(self) -> None:
        self.desabilitar_botoes()
        if self.mensagem is not None:
            try:
                await self.mensagem.edit(view=self)
            except discord.HTTPException:
                pass


class InvestimentosCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        banco = conectar_banco()
        banco.close()
        self.verificar_alertas.start()

    def cog_unload(self) -> None:
        self.verificar_alertas.cancel()

    @tasks.loop(minutes=15)
    async def verificar_alertas(self) -> None:
        banco = conectar_banco()
        try:
            alertas = banco.execute(
                """
                SELECT alertas.usuario_id, alertas.ticker, alertas.limite_percentual,
                       alertas.preco_referencia, alertas.alerta_ativa
                FROM investimento_demo_alertas AS alertas
                JOIN investimento_demo_posicoes AS posicoes
                  ON posicoes.usuario_id = alertas.usuario_id
                 AND posicoes.ticker = alertas.ticker
                """
            ).fetchall()
        finally:
            banco.close()
        if not alertas:
            return

        tickers = {str(alerta[1]) for alerta in alertas}
        try:
            cotacoes = await asyncio.gather(
                *(consultar_cotacao(ticker) for ticker in tickers)
            )
        except RuntimeError:
            return
        cotacoes_por_ticker = {
            str(cotacao["ticker"]): cotacao for cotacao in cotacoes
        }
        notificacoes = []
        banco = conectar_banco()
        try:
            banco.execute("BEGIN IMMEDIATE")
            for usuario_id, ticker, limite_texto, referencia_texto, alerta_ativa in alertas:
                preco = Decimal(str(cotacoes_por_ticker[str(ticker)]["preco"]))
                referencia, ativa, notificar = avaliar_queda(
                    preco,
                    Decimal(str(referencia_texto)),
                    Decimal(str(limite_texto)),
                    bool(alerta_ativa),
                )
                banco.execute(
                    """
                    UPDATE investimento_demo_alertas
                    SET preco_referencia = ?, alerta_ativa = ?
                    WHERE usuario_id = ? AND ticker = ?
                    """,
                    (str(referencia), int(ativa), usuario_id, ticker),
                )
                if notificar:
                    queda = (referencia - preco) / referencia * 100
                    notificacoes.append((int(usuario_id), str(ticker), preco, referencia, queda))
            banco.commit()
        except Exception:
            banco.rollback()
            raise
        finally:
            banco.close()

        for usuario_id, ticker, preco, referencia, queda in notificacoes:
            try:
                usuario = self.bot.get_user(usuario_id)
                if usuario is None:
                    usuario = await self.bot.fetch_user(usuario_id)
                view = AlertaQuedaView(usuario_id, ticker)
                mensagem = await usuario.send(
                    f"Alerta de queda na sua simulação: **{ticker}** caiu "
                    f"{formatar_percentual(queda)} desde a máxima observada de "
                    f"{formatar_reais(referencia)} e está em {formatar_reais(preco)}. "
                    "Isso não prevê novas quedas. Quer vender todas as cotas desta posição "
                    "na simulação ou manter o ativo?"
                    ,
                    view=view,
                )
                view.mensagem = mensagem
            except discord.HTTPException:
                continue

    @verificar_alertas.before_loop
    async def aguardar_bot_pronto(self) -> None:
        await self.bot.wait_until_ready()

    @commands.group(name="investir", invoke_without_command=True, hidden=True)
    async def investir(self, ctx: commands.Context) -> None:
        await ctx.reply(
            "Use `!investir analisar <valor> <conservador|moderado|arrojado>` "
            "`!investir carteira`, `!investir alertas` ou `!investir simular`."
        )

    @investir.command(name="analisar")
    async def analisar(self, ctx: commands.Context, valor_texto: str, perfil: str) -> None:
        valor = para_decimal(valor_texto)
        perfil = perfil.casefold()
        if valor is None:
            await ctx.reply("Informe um valor positivo, por exemplo `!investir analisar 500 moderado`.")
            return
        if perfil not in PERFIS:
            await ctx.reply("Perfil inválido. Escolha `conservador`, `moderado` ou `arrojado`.")
            return

        try:
            tickers_etf = sorted(
                ticker
                for composicao in PERFIS.values()
                for ticker in composicao
                if ticker in ATIVOS
            )
            cotacoes = await asyncio.gather(
                *(consultar_cotacao(ticker) for ticker in set(tickers_etf)),
                consultar_tesouro_selic(),
            )
        except RuntimeError as erro:
            await ctx.reply(f"Análise indisponível: {erro}.")
            return

        por_ticker = {
            str(cotacao["ticker"]): cotacao
            for cotacao in cotacoes
            if "ticker" in cotacao
        }
        tesouro = next(cotacao for cotacao in cotacoes if "taxa_compra" in cotacao)
        linhas = []
        for ticker, percentual in PERFIS[perfil].items():
            alocacao = valor * Decimal(percentual) / 100
            if ticker == "TESOURO_SELIC":
                taxa = f"{tesouro['taxa_compra']:+.2f}".replace(".", ",")
                linhas.append(
                    f"**Tesouro Selic 2031** ({percentual}%, {formatar_reais(alocacao)}) | "
                    f"preço indicativo por título inteiro: {formatar_reais(tesouro['preco'])} | "
                    f"taxa de compra: {taxa}% a.a. sobre a Selic"
                )
                continue
            cotacao = por_ticker[ticker]
            variacao = cotacao["variacao_mes"]
            texto_variacao = "sem histórico suficiente" if variacao is None else f"{variacao:+.2f}% em ~1 mês"
            linhas.append(
                f"**{ticker}** ({percentual}%, {formatar_reais(alocacao)}) | "
                f"{formatar_reais(cotacao['preco'])}/cota | {texto_variacao}"
            )

        embed = discord.Embed(
            title=f"Análise indicativa | perfil {perfil}",
            description="\n".join(linhas),
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="Antes de investir",
            value=(
                "Use apenas dinheiro que não comprometa suas contas. Quite dívidas caras e "
                "priorize uma reserva de emergência com liquidez. A divisão é educativa, "
                "não considera seus objetivos e não é recomendação personalizada. "
                "A variação mensal é apenas contexto histórico, não um sinal de compra."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"ETFs: Yahoo Finance ({cotacoes[0]['atualizado_em']}); "
                f"Tesouro Selic: brapi.dev (base {tesouro['data_base']}). "
                "Rentabilidade passada não prevê resultados futuros."
            )
        )
        await ctx.reply(embed=embed)

    @investir.command(name="alertas")
    async def configurar_alertas(
        self, ctx: commands.Context, limite_texto: str | None = None
    ) -> None:
        if limite_texto is not None and limite_texto.casefold() in {"off", "desligar"}:
            banco = conectar_banco()
            try:
                banco.execute(
                    "DELETE FROM investimento_demo_alertas WHERE usuario_id = ?",
                    (ctx.author.id,),
                )
                banco.commit()
            finally:
                banco.close()
            await ctx.reply("Alertas de queda desativados para sua carteira simulada.")
            return

        limite = Decimal("5") if limite_texto is None else para_decimal(limite_texto)
        if limite is None or limite > 50:
            await ctx.reply(
                "Informe um limite maior que 0 e até 50%, por exemplo `!investir alertas 5`, "
                "ou use `!investir alertas desligar`."
            )
            return

        banco = conectar_banco()
        try:
            posicoes = banco.execute(
                "SELECT ticker FROM investimento_demo_posicoes WHERE usuario_id = ?",
                (ctx.author.id,),
            ).fetchall()
        finally:
            banco.close()
        if not posicoes:
            await ctx.reply(
                "Você ainda não tem ativos na carteira simulada. Compre uma cota antes de ativar alertas."
            )
            return

        tickers = {str(posicao[0]) for posicao in posicoes}
        try:
            cotacoes = await asyncio.gather(
                *(consultar_cotacao(ticker) for ticker in tickers)
            )
        except RuntimeError as erro:
            await ctx.reply(f"Não consegui ativar os alertas: {erro}.")
            return

        banco = conectar_banco()
        try:
            banco.execute("BEGIN IMMEDIATE")
            for cotacao in cotacoes:
                banco.execute(
                    """
                    INSERT INTO investimento_demo_alertas
                        (usuario_id, ticker, limite_percentual, preco_referencia, alerta_ativa)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(usuario_id, ticker) DO UPDATE SET
                        limite_percentual = excluded.limite_percentual,
                        preco_referencia = excluded.preco_referencia,
                        alerta_ativa = 0
                    """,
                    (
                        ctx.author.id,
                        cotacao["ticker"],
                        str(limite),
                        str(cotacao["preco"]),
                    ),
                )
            banco.commit()
        except Exception:
            banco.rollback()
            raise
        finally:
            banco.close()
        lista_tickers = ", ".join(sorted(tickers))
        await ctx.reply(
            f"Alertas ativados para **{lista_tickers}**: aviso ao atingir queda de "
            f"{formatar_percentual(limite)} desde a máxima observada. "
            "Verifico a cada 15 minutos e envio por DM; mantenha as mensagens diretas abertas. "
            "Use `!investir alertas desligar` para cancelar."
        )

    @investir.command(name="carteira")
    async def carteira(self, ctx: commands.Context) -> None:
        banco = conectar_banco()
        try:
            banco.execute("BEGIN IMMEDIATE")
            saldo = obter_conta(banco, ctx.author.id)
            posicoes = banco.execute(
                "SELECT ticker, quantidade, custo_centavos FROM investimento_demo_posicoes "
                "WHERE usuario_id = ? ORDER BY ticker",
                (ctx.author.id,),
            ).fetchall()
            banco.commit()
        finally:
            banco.close()

        saldo_reais = Decimal(saldo) / 100
        cotacoes_por_ticker = {}
        if posicoes:
            try:
                cotacoes = await asyncio.gather(
                    *(consultar_cotacao(str(posicao[0])) for posicao in posicoes)
                )
            except RuntimeError as erro:
                await ctx.reply(f"Não consegui atualizar a carteira: {erro}.")
                return
            cotacoes_por_ticker = {
                str(cotacao["ticker"]): cotacao for cotacao in cotacoes
            }

        valores_atuais = {
            str(ticker): cotacoes_por_ticker[str(ticker)]["preco"] * int(quantidade)
            for ticker, quantidade, _ in posicoes
        }
        total_investido = sum(valores_atuais.values(), Decimal(0))
        custo_total = sum(
            (Decimal(int(custo_centavos)) / 100 for _, _, custo_centavos in posicoes),
            Decimal(0),
        )
        patrimonio = saldo_reais + total_investido

        embed = discord.Embed(
            title=f"Carteira simulada de {ctx.author.display_name}",
            description="Percentuais calculados sobre o patrimônio total: saldo fictício + ativos.",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Total investido (valor atual)",
            value=formatar_reais(total_investido),
            inline=True,
        )
        embed.add_field(
            name="Custo de aquisição",
            value=formatar_reais(custo_total),
            inline=True,
        )
        embed.add_field(
            name="Saldo fictício",
            value=formatar_reais(saldo_reais),
            inline=True,
        )
        embed.add_field(
            name="Patrimônio simulado",
            value=f"**{formatar_reais(patrimonio)}**",
            inline=True,
        )

        if posicoes:
            for ticker, quantidade, custo_centavos in posicoes:
                valor_atual = valores_atuais[str(ticker)]
                custo = Decimal(int(custo_centavos)) / 100
                percentual = valor_atual / patrimonio * 100 if patrimonio else Decimal(0)
                resultado = valor_atual - custo
                percentual_resultado = (
                    resultado / custo * 100 if custo else Decimal(0)
                )
                embed.add_field(
                    name=f"{ticker} | {formatar_percentual(percentual)} do patrimônio",
                    value=(
                        f"{quantidade} cota(s) | valor atual: {formatar_reais(valor_atual)}\n"
                        f"Custo: {formatar_reais(custo)} | resultado: "
                        f"{formatar_reais(resultado)} ({formatar_percentual(percentual_resultado)})"
                    ),
                    inline=False,
                )
            atualizado_em = cotacoes[0]["atualizado_em"]
            embed.set_footer(
                text=f"Cotações indicativas via Yahoo Finance, consultadas em {atualizado_em}. "
                "Valores da carteira são fictícios."
            )
        else:
            embed.add_field(
                name="Investimentos",
                value="Nenhuma cota na carteira. Use `!investir simular comprar <ativo> <valor>`.",
                inline=False,
            )
            embed.set_footer(text="Carteira de simulação; nenhum dinheiro real está envolvido.")

        await ctx.reply(embed=embed)

    @investir.group(name="simular", invoke_without_command=True)
    async def simular(self, ctx: commands.Context) -> None:
        banco = conectar_banco()
        try:
            banco.execute("BEGIN IMMEDIATE")
            saldo = obter_conta(banco, ctx.author.id)
            posicoes = banco.execute(
                "SELECT ticker, quantidade, custo_centavos FROM investimento_demo_posicoes "
                "WHERE usuario_id = ? ORDER BY ticker",
                (ctx.author.id,),
            ).fetchall()
            banco.commit()
        finally:
            banco.close()

        linhas = [f"Saldo fictício: **{formatar_reais(Decimal(saldo) / 100)}**"]
        total_carteira = Decimal(saldo) / 100
        if posicoes:
            try:
                cotacoes = await asyncio.gather(
                    *(consultar_cotacao(str(posicao[0])) for posicao in posicoes)
                )
            except RuntimeError as erro:
                await ctx.reply(f"Não consegui atualizar a carteira: {erro}.")
                return
            por_ticker = {str(cotacao["ticker"]): cotacao for cotacao in cotacoes}
            for ticker, quantidade, custo_centavos in posicoes:
                cotacao = por_ticker[str(ticker)]
                valor_atual = cotacao["preco"] * int(quantidade)
                custo = Decimal(int(custo_centavos)) / 100
                total_carteira += valor_atual
                linhas.append(
                    f"**{ticker}**: {quantidade} cota(s) | valor {formatar_reais(valor_atual)} | "
                    f"resultado {formatar_reais(valor_atual - custo)}"
                )
        else:
            linhas.append("Nenhuma cota comprada ainda.")
        linhas.append(f"Patrimônio simulado: **{formatar_reais(total_carteira)}**")
        linhas.append("Ativos disponíveis: `LFTS11`, `BOVA11` e `IVVB11`.")
        await ctx.reply("\n".join(linhas))

    @simular.command(name="comprar")
    async def comprar(self, ctx: commands.Context, ticker_texto: str, valor_texto: str) -> None:
        ticker = normalizar_ticker(ticker_texto)
        valor = para_decimal(valor_texto)
        if ticker is None:
            await ctx.reply("Ativo inválido. Use `LFTS11`, `BOVA11` ou `IVVB11`.")
            return
        if valor is None:
            await ctx.reply("Informe um valor positivo, por exemplo `!investir simular comprar BOVA11 500`.")
            return
        try:
            cotacao = await consultar_cotacao(ticker)
        except RuntimeError as erro:
            await ctx.reply(f"Compra simulada indisponível: {erro}.")
            return

        preco_centavos = int((cotacao["preco"] * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        valor_centavos = int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        banco = conectar_banco()
        try:
            banco.execute("BEGIN IMMEDIATE")
            saldo = obter_conta(banco, ctx.author.id)
            quantidade = min(valor_centavos, saldo) // preco_centavos
            if quantidade < 1:
                banco.rollback()
                await ctx.reply("Saldo fictício insuficiente para comprar uma cota desse ativo.")
                return
            custo = quantidade * preco_centavos
            banco.execute(
                "UPDATE investimento_demo_contas SET saldo_centavos = saldo_centavos - ? WHERE usuario_id = ?",
                (custo, ctx.author.id),
            )
            banco.execute(
                """
                INSERT INTO investimento_demo_posicoes (usuario_id, ticker, quantidade, custo_centavos)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(usuario_id, ticker) DO UPDATE SET
                    quantidade = quantidade + excluded.quantidade,
                    custo_centavos = custo_centavos + excluded.custo_centavos
                """,
                (ctx.author.id, ticker, quantidade, custo),
            )
            banco.commit()
        except Exception:
            banco.rollback()
            raise
        finally:
            banco.close()
        await ctx.reply(
            f"Compra fictícia: **{quantidade} {ticker}** por {formatar_reais(Decimal(custo) / 100)}. "
            f"Cotação consultada: {formatar_reais(cotacao['preco'])}/cota."
        )

    @simular.command(name="vender")
    async def vender(self, ctx: commands.Context, ticker_texto: str, quantidade_texto: str) -> None:
        ticker = normalizar_ticker(ticker_texto)
        if ticker is None:
            await ctx.reply("Ativo inválido. Use `LFTS11`, `BOVA11` ou `IVVB11`.")
            return
        if not quantidade_texto.isdecimal() or int(quantidade_texto) < 1:
            await ctx.reply("Informe uma quantidade inteira positiva, por exemplo `!investir simular vender BOVA11 1`.")
            return
        quantidade = int(quantidade_texto)
        try:
            cotacao = await consultar_cotacao(ticker)
        except RuntimeError as erro:
            await ctx.reply(f"Venda simulada indisponível: {erro}.")
            return

        preco_centavos = int((cotacao["preco"] * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        venda = registrar_venda_simulada(
            ctx.author.id, ticker, quantidade, preco_centavos
        )
        if venda is None:
            await ctx.reply("Você não possui essa quantidade de cotas na carteira fictícia.")
            return
        quantidade_vendida, recebido, resultado = venda
        await ctx.reply(
            f"Venda fictícia: **{quantidade_vendida} {ticker}** por {formatar_reais(Decimal(recebido) / 100)}. "
            f"Resultado realizado simulado: {formatar_reais(Decimal(resultado) / 100)}."
        )

    @simular.command(name="reiniciar")
    async def reiniciar(self, ctx: commands.Context, confirmacao: str) -> None:
        if confirmacao.casefold() != "confirmar":
            await ctx.reply("Para reiniciar a carteira fictícia, use `!investir simular reiniciar CONFIRMAR`.")
            return
        banco = conectar_banco()
        try:
            banco.execute("BEGIN IMMEDIATE")
            banco.execute("DELETE FROM investimento_demo_posicoes WHERE usuario_id = ?", (ctx.author.id,))
            banco.execute("DELETE FROM investimento_demo_alertas WHERE usuario_id = ?", (ctx.author.id,))
            banco.execute(
                """
                INSERT INTO investimento_demo_contas (usuario_id, saldo_centavos) VALUES (?, ?)
                ON CONFLICT(usuario_id) DO UPDATE SET saldo_centavos = excluded.saldo_centavos
                """,
                (ctx.author.id, SALDO_INICIAL_CENTAVOS),
            )
            banco.commit()
        except Exception:
            banco.rollback()
            raise
        finally:
            banco.close()
        await ctx.reply("Carteira fictícia reiniciada com R$ 10.000,00.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InvestimentosCog(bot))