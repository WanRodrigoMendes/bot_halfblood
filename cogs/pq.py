import discord
from discord.ext import commands


class PorquesCog(commands.Cog):
    @commands.command(name="pq")
    async def porques(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="Uso dos porquês",
            color=discord.Color.from_rgb(70, 120, 145),
        )
        embed.add_field(
            name="Por que",
            value=(
                "Use em perguntas ou quando for possível substituir por "
                "**razão** ou **motivo**.\n"
                "Exemplo: *Por que você saiu?*"
            ),
            inline=False,
        )
        embed.add_field(
            name="Por quê",
            value=(
                "Use no final da frase, com o sentido de **por qual razão**.\n"
                "Exemplo: *Você saiu por quê?*"
            ),
            inline=False,
        )
        embed.add_field(
            name="Porque",
            value=(
                "Use em respostas e afirmações; tem o mesmo valor de **pois**.\n"
                "Exemplo: *Saí porque estava tarde.*"
            ),
            inline=False,
        )
        embed.add_field(
            name="Porquê",
            value=(
                "É sinônimo de **motivo** e vem precedido de artigo ou numeral.\n"
                "Exemplo: *Não entendi o porquê da saída.*"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PorquesCog())