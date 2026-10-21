import discord
from discord.ext import commands

from sistemas.forjas.interface import normalizar
from sistemas.forjas.materiais import MATERIAIS, MISTURAS

ITENS_POR_PAGINA = 8
NOMES_EFEITOS = {
    "bonus_fabricacao": "Bônus de fabricação",
    "corrosao_reducao": "Redução da corrosão",
    "resistencia_corrosao": "Resistência à corrosão",
    "resistencia_fogo": "Resistência ao fogo",
    "resistencia_calor": "Resistência ao calor",
    "resistencia_oxidacao": "Resistência à oxidação",
    "resistencia": "Resistência",
    "durabilidade": "Durabilidade",
    "dano_igneo": "Dano ígneo",
    "dano_eletrico": "Dano elétrico",
    "dano_termico": "Dano térmico",
    "dano_contusao": "Dano por contusão",
    "bonus_projetil": "Bônus para projéteis",
    "canalizacao_magica": "Canalização mágica",
    "canalizacao_terra": "Canalização de terra",
    "canalizacao_agua": "Canalização de água",
    "canalizacao_fogo": "Canalização de fogo",
    "canalizacao_vento": "Canalização de vento",
    "canalizacao_elemental": "Canalização elemental",
    "inoxidavel": "Inoxidável",
}


def listar_materiais() -> list[tuple[str, dict]]:
    return sorted(
        {**MATERIAIS, **MISTURAS}.items(),
        key=lambda item: item[1]["nome_exibicao"].casefold(),
    )


def pesquisar_materiais(consulta: str) -> list[tuple[str, dict]]:
    materiais = listar_materiais()
    resultados = []
    encontrados = set()

    for termo in consulta.split(","):
        consulta_normalizada = normalizar(termo.strip())
        if not consulta_normalizada:
            continue

        correspondencias = [
            (chave, dados)
            for chave, dados in materiais
            if consulta_normalizada
            in {
                normalizar(chave),
                normalizar(dados["nome_exibicao"]),
                *(normalizar(nome) for nome in dados.get("nomes", [])),
            }
        ]
        if not correspondencias:
            correspondencias = [
                (chave, dados)
                for chave, dados in materiais
                if any(
                    consulta_normalizada in normalizar(nome)
                    for nome in (
                        chave,
                        dados["nome_exibicao"],
                        *dados.get("nomes", []),
                    )
                )
            ]

        for chave, dados in correspondencias:
            if chave not in encontrados:
                resultados.append((chave, dados))
                encontrados.add(chave)

    return resultados


def formatar_efeitos(efeitos: dict) -> str:
    if not efeitos:
        return "Sem bônus cadastrado."

    linhas = []
    for efeito, valor in efeitos.items():
        nome = NOMES_EFEITOS.get(efeito, efeito.replace("_", " ").capitalize())
        if isinstance(valor, bool):
            valor_formatado = "Sim" if valor else "Não"
        elif efeito in {"corrosao_reducao", "resistencia_corrosao"}:
            valor_formatado = f"{valor:+}%"
        else:
            valor_formatado = f"{valor:+}"
        linhas.append(f"**{nome}:** {valor_formatado}")

    return "\n".join(linhas)


class MateriaisPaginados(discord.ui.View):
    def __init__(
        self,
        paginas: list[list[tuple[str, dict]]],
        autor_id: int,
        consulta: str | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.paginas = paginas
        self.autor_id = autor_id
        self.consulta = consulta
        self.pagina_atual = 0
        self.mensagem: discord.Message | None = None
        self.atualizar_botoes()

    def atualizar_botoes(self) -> None:
        self.anterior.disabled = self.pagina_atual == 0
        self.proxima.disabled = self.pagina_atual == len(self.paginas) - 1

    def criar_embed(self) -> discord.Embed:
        titulo = "Resultados da busca" if self.consulta else "Materiais e ligas"
        embed = discord.Embed(
            title=titulo,
            color=discord.Color.from_rgb(139, 90, 43),
        )
        if self.consulta:
            embed.description = f"Busca por **{self.consulta}**"

        for _, dados in self.paginas[self.pagina_atual]:
            embed.add_field(
                name=dados["nome_exibicao"],
                value=formatar_efeitos(dados.get("efeitos", {})),
                inline=False,
            )

        embed.set_footer(
            text=f"Página {self.pagina_atual + 1}/{len(self.paginas)}"
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message(
                "Somente quem iniciou a consulta pode navegar pelas páginas.",
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

    async def mudar_pagina(
        self,
        interaction: discord.Interaction,
        delta: int,
    ) -> None:
        self.pagina_atual += delta
        self.atualizar_botoes()
        await interaction.response.edit_message(embed=self.criar_embed(), view=self)

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary)
    async def anterior(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.mudar_pagina(interaction, -1)

    @discord.ui.button(label="Próxima", style=discord.ButtonStyle.primary)
    async def proxima(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.mudar_pagina(interaction, 1)


class MateriaisCog(commands.Cog):
    @commands.command(name="materiais", aliases=["material"])
    async def consultar(
        self,
        ctx: commands.Context,
        *,
        consulta: str | None = None,
    ) -> None:
        materiais = pesquisar_materiais(consulta) if consulta else listar_materiais()
        if not materiais:
            embed = discord.Embed(
                title="Material não encontrado",
                description=f"Não encontrei **{consulta}** no catálogo.",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            return

        paginas = [
            materiais[inicio : inicio + ITENS_POR_PAGINA]
            for inicio in range(0, len(materiais), ITENS_POR_PAGINA)
        ]
        view = MateriaisPaginados(paginas, ctx.author.id, consulta)
        if len(paginas) == 1:
            await ctx.send(embed=view.criar_embed())
            return

        mensagem = await ctx.send(embed=view.criar_embed(), view=view)
        view.mensagem = mensagem


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MateriaisCog())