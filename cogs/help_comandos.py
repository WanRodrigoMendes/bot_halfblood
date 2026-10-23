import discord
from discord.ext import commands

COMMAND_DETAILS = {
    "apv": (
        "responda à mensagem de uma ficha.",
        "-# > Aprova uma ficha no servidor grego ou romano, usando o canal, cargos e palavras-chave configurados para cada servidor.",
    ),
    "materiais": (
        "[nome] (ou !material [nome])",
        "-# > Pesquisa os bônus de um material ou lista os materiais e ligas disponíveis.",
    ),
    "fazerlvl": (
        "<informações> <@membro> <XP do nível 1>",
        "-# > Gera uma tabela de níveis no canal configurado. Requer permissão de administrador ou gerenciar mensagens.",
    ),
    "pq": (
        "",
        "-# > Mostra quando usar por que, por quê, porque e porquê.",
    ),
    "minerar": (
        "",
        "-# > Faz uma mineração, limitada a duas tentativas por dia em cada servidor.",
    ),
    "minerar-pedras-preciosas": (
        "",
        "-# > Busca pedras preciosas, limitada a duas tentativas por dia em cada servidor.",
    ),
    "expedicao": (
        "<npc [adicionar | remover] | objetivo | objetivos | contratar | status>",
        "-# > Mod/ADM cadastra objetivos e recompensas; jogadores contratam até três NPCs. "
        "Mods/ADMs também podem adicionar ou remover NPCs por servidor. "
        "Poder afeta sucesso e baixas, sorte afeta loot, exploração reduz o tempo e NPCs mortos não retornam.",
    ),
    # "investir": (
    #     "<analisar | simular>",
    #     "-# > Consulta cotações indicativas e apresenta uma divisão educativa por perfil; também permite comprar e vender ETFs com saldo fictício.",
    # ),
    # "stbench": (
    #     "<qntd. moedas> <preço por rodada> <valor máximo> [cara | coroa]",
    #     "-# > Joga uma moeda para o alto e paga 2^n moedas quando a sequência termina (quando perde na sua escolha). A sessão para ao alcançar a meta ou zerar o saldo.",
    # ),
    "help": ("", "-# > Mostra esta lista de comandos."),
}


class HelpCommands(commands.Cog):
    @commands.command(name="help")
    async def show_help(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="Comandos disponíveis",
            color=discord.Color.blurple(),
        )
        for command in sorted(ctx.bot.commands, key=lambda registered: registered.name):
            if command.hidden or command.name == "stbench":
                continue
            usage, description = COMMAND_DETAILS.get(
                command.name,
                (command.signature, command.help or "Sem descrição disponível."),
            )
            invocation = f"{ctx.clean_prefix}{command.name} {usage}".rstrip()
            embed.add_field(name=invocation, value=description, inline=False)

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCommands())