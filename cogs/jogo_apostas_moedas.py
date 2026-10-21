import random

import discord
from discord.ext import commands

APOSTA_MINIMA = 8
PROBABILIDADE_ACERTO = 50
ITENS_POR_PAGINA = 10
SEGUNDOS_POR_LANCAMENTO = 3


def formatar_moedas(valor: int) -> str:
    return f"{valor:,}".replace(",", ".")


def simular_entrada(lado: str) -> tuple[int, int, str, int]:
    lado_oposto = "coroa" if lado == "cara" else "cara"
    vitorias = 0

    while True:
        if random.randint(1, 100) <= PROBABILIDADE_ACERTO:
            vitorias += 1
            continue

        lancamentos = vitorias + 1
        premio = 2**lancamentos
        return vitorias, lancamentos, lado_oposto, premio


class ResultadoPaginado(discord.ui.View):
    def __init__(
        self,
        paginas: list[list[str]],
        autor_id: int,
        saldo_inicial: int,
        aposta_por_entrada: int,
        meta: int,
        saldo_final: int,
        total_lancamentos: int,
        total_rodadas: int,
        estado: str,
    ) -> None:
        super().__init__(timeout=180)
        self.paginas = paginas
        self.autor_id = autor_id
        self.saldo_inicial = saldo_inicial
        self.aposta_por_entrada = aposta_por_entrada
        self.meta = meta
        self.saldo_final = saldo_final
        self.total_lancamentos = total_lancamentos
        self.total_rodadas = total_rodadas
        self.estado = estado
        self.pagina_atual = 0
        self.mensagem: discord.Message | None = None
        self.atualizar_botoes()

    def atualizar_botoes(self) -> None:
        self.anterior.disabled = self.pagina_atual == 0
        self.proxima.disabled = self.pagina_atual >= len(self.paginas) - 1

    def criar_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="The Petersburg Bench",
            description="\n\n".join(self.paginas[self.pagina_atual]),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="\u200b",
            value="ㅤㅤ￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣",
            inline=False,
        )
        embed.add_field(
            name="Saldo inicial:",
            value=f"{formatar_moedas(self.saldo_inicial)} moedas",
            inline=True,
        )
        embed.add_field(
            name="Por entrada:",
            value=f"{formatar_moedas(self.aposta_por_entrada)} moedas",
            inline=True,
        )
        embed.add_field(
            name="Meta:",
            value=f"{formatar_moedas(self.meta)} moedas",
            inline=True,
        )
        embed.add_field(
            name="Saldo final:",
            value=f"{formatar_moedas(self.saldo_final)} moedas",
            inline=True,
        )
        embed.add_field(name="Rodadas", value=str(self.total_rodadas), inline=True)
        duracao_segundos = self.total_lancamentos * SEGUNDOS_POR_LANCAMENTO
        horas, segundos_restantes = divmod(duracao_segundos, 3600)
        minutos, segundos = divmod(segundos_restantes, 60)
        embed.add_field(
            name="Duração estimada",
            value=f"{formatar_moedas(horas)} h, {minutos} min e {segundos} s",
            inline=True,
        )
        embed.add_field(name="Encerramento", value=self.estado, inline=True)
        embed.set_footer(text=f"Página {self.pagina_atual + 1}/{len(self.paginas)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message(
                "Somente quem iniciou a simulação pode navegar pelas páginas.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.mensagem is not None:
            try:
                await self.mensagem.edit(view=self)
            except discord.HTTPException:
                pass

    async def mudar_pagina(self, interaction: discord.Interaction, delta: int) -> None:
        self.pagina_atual += delta
        self.atualizar_botoes()
        await interaction.response.edit_message(embed=self.criar_embed(), view=self)

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary)
    async def anterior(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.mudar_pagina(interaction, -1)

    @discord.ui.button(label="Próxima", style=discord.ButtonStyle.primary)
    async def proxima(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.mudar_pagina(interaction, 1)


class JogoApostasCog(commands.Cog):
    @commands.command(name="stbench")
    async def apostar(
        self,
        ctx: commands.Context,
        saldo_inicial: int,
        aposta_por_entrada: int,
        meta: int,
        lado: str = "cara",
    ) -> None:
        lado = lado.casefold()
        if lado not in {"cara", "coroa"}:
            await ctx.reply("Escolha `cara` ou `coroa` como lado favorecido.")
            return
        if saldo_inicial < APOSTA_MINIMA:
            await ctx.reply(
                f"O total disponível precisa ser de pelo menos {APOSTA_MINIMA} moedas."
            )
            return
        if aposta_por_entrada < APOSTA_MINIMA:
            await ctx.reply(f"A aposta mínima é de {APOSTA_MINIMA} moedas.")
            return
        if aposta_por_entrada > saldo_inicial:
            await ctx.reply(
                "O valor por entrada não pode superar o total disponível de "
                f"{formatar_moedas(saldo_inicial)} moedas."
            )
            return
        if meta <= saldo_inicial:
            await ctx.reply(
                "A meta precisa ser maior que o total disponível de "
                f"{formatar_moedas(saldo_inicial)} moedas."
            )
            return

        saldo = saldo_inicial
        registros: list[str] = []
        total_lancamentos = 0

        while saldo >= aposta_por_entrada and saldo < meta:
            numero_rodada = len(registros) + 1
            aposta_rodada = aposta_por_entrada
            saldo_anterior = saldo
            saldo_apos_aposta = saldo_anterior - aposta_rodada
            vitorias, lancamentos, resultado, ganho_total = simular_entrada(lado)
            total_lancamentos += lancamentos
            saldo = saldo_apos_aposta + ganho_total
            registros.append(
                f"**#{numero_rodada}** | {formatar_moedas(saldo_anterior)} → "
                f"{formatar_moedas(saldo)} moedas | "
                f"{lancamentos} lançamentos → {vitorias} acerto(s) | "
                f"**Prêmio:** {formatar_moedas(ganho_total)} moedas."
            )

        if saldo >= meta:
            estado = "Meta atingida"
        else:
            estado = "Saldo insuficiente para nova entrada"

        paginas = [
            registros[inicio : inicio + ITENS_POR_PAGINA]
            for inicio in range(0, len(registros), ITENS_POR_PAGINA)
        ]
        view = ResultadoPaginado(
            paginas=paginas,
            autor_id=ctx.author.id,
            saldo_inicial=saldo_inicial,
            aposta_por_entrada=aposta_por_entrada,
            meta=meta,
            saldo_final=saldo,
            total_lancamentos=total_lancamentos,
            total_rodadas=len(registros),
            estado=estado,
        )
        view.mensagem = await ctx.send(embed=view.criar_embed(), view=view)

    @apostar.error
    async def erro_aposta(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.reply(
                "Uso: `!stbench <total> <por entrada> <meta> [cara|coroa]`. "
                "Exemplo: `!stbench 1000 50 1500 coroa`."
            )
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JogoApostasCog())