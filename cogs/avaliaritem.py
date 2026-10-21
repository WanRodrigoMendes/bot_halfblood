import re
import unicodedata

import discord
from discord.ext import commands

from sistemas.forjas.interface import (
    calcular_bonus,
    gerar_descricao_simplificada,
    gerar_descricao_tecnica,
    gerar_resumo_materiais,
    identificar_materiais,
    identificar_misturas,
)
FIELD_PATTERN = re.compile(
    r"\*?\s*(Matéria prima|Matéria-prima|Matéria secundária|Adorno|"
    r"Refinamento|Componentes extras)\s*:\s*\*?\s*(.*)",
    re.IGNORECASE,
)
NAME_PATTERN = re.compile(r"Nome do Item\s*:\s*(.*)", re.IGNORECASE)
QUALITY_PATTERN = re.compile(r"Qualidade do item\s*:\s*(.*)", re.IGNORECASE)
COR_EMBED_AVALIACAO = discord.Color.from_rgb(139, 90, 43)


def normalizar_texto(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto.casefold())
    return "".join(
        caractere
        for caractere in texto
        if unicodedata.category(caractere) != "Mn"
    )


def obter_bonus_qualidade(qualidade: str) -> tuple[str, int]:
    qualidade_normalizada = normalizar_texto(qualidade)
    if re.search(r"\bperfeito\b", qualidade_normalizada):
        return "Perfeito", 4
    if re.search(r"\botimo\b", qualidade_normalizada):
        return "Ótimo", 2
    if re.search(r"\bbom\b", qualidade_normalizada):
        return "Bom", 1
    return qualidade.strip(), 0


def avaliar_mensagem(conteudo: str) -> str:
    materiais_declarados = []
    nome_item = "Item"
    qualidade = ""

    for linha in conteudo.splitlines():
        correspondencia = FIELD_PATTERN.search(linha)
        if correspondencia:
            materiais_declarados.append(correspondencia.group(2))

        correspondencia_nome = NAME_PATTERN.search(linha)
        if correspondencia_nome:
            nome_item = correspondencia_nome.group(1).strip().strip("*_") or "Item"

        correspondencia_qualidade = QUALITY_PATTERN.search(linha)
        if correspondencia_qualidade:
            qualidade = correspondencia_qualidade.group(1).strip().strip("*_")
            if qualidade in {".", "-", "—"}:
                qualidade = ""

    if not materiais_declarados:
        return "Não encontrei os campos de materiais da ficha. Responda à ficha completa com `!avaliaritem`."

    texto_materiais = " ".join(materiais_declarados)
    materiais = identificar_materiais(texto_materiais)
    materiais_finais, misturas = identificar_misturas(materiais)
    bonus = calcular_bonus(materiais_finais)
    qualidade_exibicao, bonus_manuseio = obter_bonus_qualidade(qualidade)

    if not materiais:
        return "Não reconheci nenhum material nos campos da ficha. Confira os nomes e tente novamente."

    descricao_tecnica = gerar_descricao_tecnica(materiais_finais, bonus)
    descricao_simplificada = gerar_descricao_simplificada(bonus)
    materiais_texto = gerar_resumo_materiais(materiais)

    materiais_especiais = {
        "ouro imperial": "Ouro imperial (romano)",
        "bronze celestial": "Bronze celestial (grego)",
        "prata lunar": "prata lunar",
        "ferro estígeo": "Ferro Estígeo",
    }
    divinos = [
        nome
        for material, nome in materiais_especiais.items()
        if material in materiais
    ]

    if divinos and bonus_manuseio:
        descricao_simplificada = descricao_simplificada.rstrip(")")
        separador = "; " if descricao_simplificada.startswith("(") else " ("
        descricao_simplificada = (
            f"{descricao_simplificada}{separador}"
            f"+{bonus_manuseio} de bônus no manuseio)"
        )

    resultado = [
        f"**Avaliação de {nome_item}**",
        f"-# > **⬩ Materiais reconhecidos:** {materiais_texto}.",
        f"-# > **⬩ Qualidade do item:** {qualidade or 'não informada'}",
        f"-# > **⬩ Qntd. bônus técnico:** {descricao_tecnica}",
        f"-# > **⬩ Qntd. bônus simplificado:** {descricao_simplificada}",
    ]
    return "\n".join(resultado)


def criar_embed_avaliacao(conteudo: str) -> discord.Embed:
    return discord.Embed(
        description=avaliar_mensagem(conteudo),
        color=COR_EMBED_AVALIACAO,
    )


class AvaliarItemCog(commands.Cog):
    @commands.command(name="avaliaritem")
    async def avaliar_item(self, ctx: commands.Context) -> None:
        referencia = ctx.message.reference
        if referencia is None or referencia.message_id is None:
            await ctx.reply("Use `!avaliaritem` respondendo à mensagem da ficha do item.")
            return

        mensagem = referencia.resolved
        if not isinstance(mensagem, discord.Message):
            try:
                mensagem = await ctx.channel.fetch_message(referencia.message_id)
            except discord.HTTPException:
                await ctx.reply("Não consegui encontrar a mensagem da ficha.")
                return

        await ctx.reply(
            embed=criar_embed_avaliacao(mensagem.content),
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AvaliarItemCog())