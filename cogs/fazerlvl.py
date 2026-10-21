import json
import re
from pathlib import Path

import discord
from discord.ext import commands

CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "fazerlvl_config.json"
MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
XP_PATTERN = re.compile(r"(?:n[ií]vel\s*1\s*=\s*)?([\d.,]+)", re.IGNORECASE)


def load_destinations() -> dict[int, int]:
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("destinations"), dict):
        raise ValueError("'destinations' precisa ser um objeto JSON.")

    destinations: dict[int, int] = {}
    for guild_id, channel_id in data["destinations"].items():
        if not isinstance(guild_id, str) or not guild_id.isdigit():
            raise ValueError("Cada servidor precisa ser identificado pelo ID numérico.")
        if type(channel_id) is not int or channel_id <= 0:
            raise ValueError("Cada canal precisa ser identificado por um ID numérico.")
        destinations[int(guild_id)] = channel_id

    return destinations


class LevelTableCog(commands.Cog):
    @commands.command(name="fazerlvl")
    async def fazer_lvl(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return

        if not isinstance(ctx.author, discord.Member):
            return
        if not (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_messages
        ):
            await ctx.reply("Somente moderadores podem usar este comando.")
            return

        payload_start = len(ctx.prefix) + len(ctx.invoked_with)
        payload = ctx.message.content[payload_start:].strip()
        mentions = list(MENTION_PATTERN.finditer(payload))
        if len(mentions) != 1:
            await ctx.send(
                "Uso: `!fazerlvl <informações> @membro <XP do nível 1>`, "
                "respondendo no servidor onde a tabela será enviada."
            )
            return

        mention = mentions[0]
        description = payload[:mention.start()].strip()
        xp_text = payload[mention.end():].strip()
        xp_match = XP_PATTERN.fullmatch(xp_text)
        if not description or xp_match is None:
            await ctx.send(
                "Informe a descrição antes da menção e o XP do nível 1 depois dela. "
                "Exemplo: `!fazerlvl Nome e informações @membro 19.345`."
            )
            return

        member_id = int(mention.group(1))
        member = ctx.guild.get_member(member_id)
        if member is None:
            try:
                member = await ctx.guild.fetch_member(member_id)
            except discord.HTTPException:
                await ctx.send("A pessoa mencionada não foi encontrada neste servidor.")
                return

        starting_xp = int(xp_match.group(1).replace(".", "").replace(",", ""))
        lines = [description + f" (oc de {member.mention})", ""]
        for level in range(1, 11):
            xp = starting_xp + (level - 1) * 5000
            formatted_xp = f"{xp:,}".replace(",", ".")
            lines.append(f"**Nível {level}:** {formatted_xp}")
        output = "\n".join(lines)

        try:
            destinations = load_destinations()
        except (OSError, json.JSONDecodeError, ValueError):
            await ctx.send("Não consegui ler 'fazerlvl_config.json'. Confira o formato do arquivo.")
            return

        channel_id = destinations.get(ctx.guild.id)
        if channel_id is None:
            await ctx.send(
                f"Configure o canal de destino deste servidor (ID `{ctx.guild.id}`) "
                "em 'fazerlvl_config.json'."
            )
            return

        destination = ctx.guild.get_channel_or_thread(channel_id)
        if not isinstance(destination, (discord.TextChannel, discord.Thread)):
            await ctx.send("O canal de destino configurado não existe ou não é um canal de texto.")
            return

        try:
            await destination.send(
                output,
                allowed_mentions=discord.AllowedMentions(
                    users=[member], roles=False, everyone=False
                ),
            )
        except discord.Forbidden:
            await ctx.send("Não tenho permissão para enviar mensagens no canal de destino.")
            return

        await ctx.send(f"Tabela de níveis enviada em {destination.mention}.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelTableCog())
