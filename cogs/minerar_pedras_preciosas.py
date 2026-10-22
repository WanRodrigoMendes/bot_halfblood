import random
import sqlite3
from datetime import datetime, time as datetime_time, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks

TENTATIVAS_DIARIAS = 2
CAMINHO_BANCO = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "mineracao_pedras_preciosas.sqlite3"
)

FAIXAS = (
    (
        40,
        (
            "quartzo rosa",
            "quartzo laranja",
            "quartzo roxo",
        ),
        5,
    ),
    (
        30,
        (
            "lápis lazuli",
            "granada",
            "peridoto",
            "zircônio",
            "aquamarine",
        ),
        4,
    ),
    (
        20,
        (
            "esmeralda",
            "rubi",
        ),
        3,
    ),
)
RESULTADOS_FIXOS = (
    ("diamante rosa", 1),
    ("diamante branco", 1),
    ("diamante azul", 1),
    ("diamante amarelo", 1),
    ("bismuto", 1),
    ("quebrou a picareta e não encontrou nada", 1),
    ("não coletou nada", 2),
)
RESULTADOS_D2 = (
    "safira",
    "minério à sua escolha",
)


def reservar_tentativa(
    servidor_id: int,
    usuario_id: int,
    hoje: str | None = None,
) -> int | None:
    dia = hoje or datetime.now(timezone.utc).date().isoformat()
    CAMINHO_BANCO.parent.mkdir(parents=True, exist_ok=True)

    banco = sqlite3.connect(CAMINHO_BANCO, timeout=10)
    try:
        banco.execute(
            """
            CREATE TABLE IF NOT EXISTS mineracao_pedras_diaria (
                servidor_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                dia TEXT NOT NULL,
                tentativas INTEGER NOT NULL CHECK (tentativas BETWEEN 1 AND 2),
                PRIMARY KEY (servidor_id, usuario_id, dia)
            )
            """
        )
        banco.execute("BEGIN IMMEDIATE")
        banco.execute(
            "DELETE FROM mineracao_pedras_diaria WHERE dia < ?",
            (dia,),
        )
        registro = banco.execute(
            """
            SELECT tentativas FROM mineracao_pedras_diaria
            WHERE servidor_id = ? AND usuario_id = ? AND dia = ?
            """,
            (servidor_id, usuario_id, dia),
        ).fetchone()
        if registro and registro[0] >= TENTATIVAS_DIARIAS:
            banco.commit()
            return None

        tentativas = registro[0] + 1 if registro else 1
        banco.execute(
            """
            INSERT INTO mineracao_pedras_diaria
                (servidor_id, usuario_id, dia, tentativas)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (servidor_id, usuario_id, dia)
            DO UPDATE SET tentativas = excluded.tentativas
            """,
            (servidor_id, usuario_id, dia, tentativas),
        )
        banco.commit()
        return tentativas
    finally:
        banco.close()


def rolar_pedra_preciosa() -> tuple[str, int, int]:
    rolagem = random.randint(1, 100)
    acumulado = 0
    for chance, materiais, quantidade_maxima in FAIXAS:
        acumulado += chance
        if rolagem <= acumulado:
            material = random.choice(materiais)
            quantidade = random.randint(1, quantidade_maxima)
            return material, quantidade, chance

    if rolagem <= acumulado + 5:
        materiais_fixos = [item for item, _ in RESULTADOS_FIXOS]
        pesos_fixos = [peso for _, peso in RESULTADOS_FIXOS]
        material = random.choices(materiais_fixos, weights=pesos_fixos, k=1)[0]
        quantidade = 0 if material == "não coletou nada" else 1
        return material, quantidade, 5

    material = random.choice(RESULTADOS_D2)
    quantidade = random.randint(1, 2)
    return material, quantidade, 5


class MinerarPedrasPreciosasCog(commands.Cog):
    async def cog_load(self) -> None:
        self.limpar_banco_diario.start()

    def cog_unload(self) -> None:
        self.limpar_banco_diario.cancel()

    @tasks.loop(time=datetime_time(hour=0, minute=0, tzinfo=timezone.utc))
    async def limpar_banco_diario(self) -> None:
        if not CAMINHO_BANCO.exists():
            return
        dia = datetime.now(timezone.utc).date().isoformat()
        banco = sqlite3.connect(CAMINHO_BANCO, timeout=10)
        try:
            banco.execute(
                "DELETE FROM mineracao_pedras_diaria WHERE dia < ?",
                (dia,),
            )
            banco.commit()
        finally:
            banco.close()

    @limpar_banco_diario.before_loop
    async def aguardar_bot_pronto(self) -> None:
        await self.bot.wait_until_ready()

    @commands.command(
        name="minerar-pedras-preciosas",
        aliases=["minerarpedras", "minerar_pedras_preciosas"],
    )
    async def minerar_pedras_preciosas(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply(
                "A mineração de pedras preciosas só pode ser usada dentro de um servidor."
            )
            return

        tentativa = reservar_tentativa(ctx.guild.id, ctx.author.id)
        if tentativa is None:
            await ctx.reply(
                f"Você já usou suas {TENTATIVAS_DIARIAS} tentativas de mineração "
                "de pedras preciosas neste servidor hoje. O limite reinicia à meia-noite UTC."
            )
            return

        material, quantidade, chance = rolar_pedra_preciosa()
        embed = discord.Embed(
            title="Mineração de pedras preciosas",
            color=discord.Color.from_rgb(139, 90, 43),
        )
        if material == "não coletou nada":
            resultado = "Você não coletou nada desta vez."
        elif material == "quebrou a picareta e não encontrou nada":
            resultado = "Sua picareta quebrou e você não encontrou nada."
        elif material == "minério à sua escolha":
            nome = "minério" if quantidade == 1 else "minérios"
            resultado = (
                f"-# > Você pode escolher **x{quantidade} {nome}** como recompensa."
            )
        else:
            nome = material if quantidade == 1 else f"{material}s"
            resultado = f"-# > Você encontrou **x{quantidade} {nome}**!"
        embed.description = resultado
        embed.set_footer(
            text=(
                f"Você rodou {tentativa}/{TENTATIVAS_DIARIAS} "
                f"no servidor {ctx.guild.name} no dia de hoje."
            )
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinerarPedrasPreciosasCog())
