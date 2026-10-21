import json
import re
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.ext import commands


@dataclass(frozen=True)
class ApprovalConfig:
    channel_id: int
    approver_role_ids: frozenset[int]
    role_to_remove_id: int
    role_keywords_file: Path
    approval_message: str


SERVER_CONFIGS = {
    958843879313014794: ApprovalConfig(
        channel_id=960038306106736700,
        approver_role_ids=frozenset({1557314795634429963, 1074022636587323432}),
        role_to_remove_id=965431462071525407,
        role_keywords_file=(
            Path(__file__).resolve().parents[1]
            / "config"
            / "role_keywords_grego.json"
        ),
        approval_message=""" 
## Ficha Aprovada!
₊˚ ‿︵‿︵‿︵‿︵‿︵‿︵ ‿︵‿︵‿︵‿︵‿︵‿︵˚₊
-# > 🏷️ - Pegue suas tags no canal <#1075141121262489701> ! 
-# > 🏡 - Pode começar turnando chegando em <#991023249666220143> e marcar os *Buscadores de Novato*.
-# > 👟 - Lembre-se, você ainda não sabe quem é seu pai ou mãe divino, mas até lá pode dormir no <#960033811519922206>. 
-# > ❓ - Qualquer dificuldade vá até canal <#960037004937138196> e marque a *Equipe de Dúvidas*.
-# > 🎚 - Caso tenha criado um oc secundário, não esqueça de solicitar os níveis no <#960037289508098078> e marcar a *Staff*.
₊˚ ‿︵‿︵‿︵‿︵‿︵‿︵ ‿︵‿︵‿︵‿︵‿︵‿︵˚₊
-# ⤷ ｢🐎｣﹒ᴇǫᴜɪᴘᴇ ᴅᴏ ᴀᴄᴀᴍᴘᴀᴍᴇɴᴛᴏ ᴍᴇɪᴏ ꜱᴀɴɢᴜᴇ - ᴠ𝟦.""",
            ),
            941036256526209183: ApprovalConfig(
                channel_id=941045862937792513,
                approver_role_ids=frozenset({1189715675153960970}),
                role_to_remove_id=941045738304045106,
                role_keywords_file=(
                    Path(__file__).resolve().parents[1]
                    / "config"
                    / "role_keywords_romano.json"
                ),
                approval_message=""" 
## ・ <:elmo:1158438321496805516> ┊ _Boas vindas ao Acampamento Júpiter, probatio! Sua ficha está **aprovada**!_
        ￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣
-# > ・ :label: ┊ Pegue suas tags no canal <#1047171389850931280>!
-# > ・ :scroll: ┊ Pode começar cenando quando quiser no canal <#1001160040415248484> e posteriormente em <#941045886459469894>.
-# > ・ <a:emoji_65:1172324123024826439> ┊ Leia as regras no canal <#941427425575862272>.
-# > ・ :question: ┊ Qualquer dificuldade vá até o canal <#1047171391155355788> e marque @⎯⎯⎯⎯⎯⎯⎯୧ ✦ Cargos Staff! ⊰ ⎯⎯⎯⎯⎯⎯.
-# > ・ :warning: ┊ Lembre-se, você ainda não sabe quem é seu pai ou mãe divino, só após a reclamação (Após 7 dias ou até chegar ao nível 2).
￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣
-# >  ⤷ ｢<a:emoji_51:1171861057061523466>｣﹒**EQUIPE DO ACAMPAMENTO JÚPITER**""",
    ),
}


def load_role_config(path: Path) -> tuple[int | None, dict[str, int]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("O arquivo precisa conter um objeto JSON.")

    approved_role_id = data.get("approved_role_id")
    if approved_role_id is not None and (
        type(approved_role_id) is not int or approved_role_id <= 0
    ):
        raise ValueError("O ID do cargo aprovado precisa ser um inteiro positivo.")

    keywords = data.get("keywords")
    if not isinstance(keywords, dict):
        raise ValueError("'keywords' precisa ser um objeto JSON.")

    role_keywords: dict[str, int] = {}
    for keyword, role_id in keywords.items():
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("As palavras-chave precisam ser textos não vazios.")
        if type(role_id) is not int or role_id <= 0:
            raise ValueError("Cada ID de cargo precisa ser um inteiro positivo.")
        role_keywords[keyword] = role_id

    return approved_role_id, role_keywords


class ApprovalCog(commands.Cog):
    @commands.command(name="apv")
    async def aprovar_ficha(self, ctx: commands.Context) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return

        config = SERVER_CONFIGS.get(ctx.guild.id)
        if config is None:
            await ctx.reply("Este comando não está configurado para este servidor.")
            return

        if ctx.channel.id != config.channel_id:
            await ctx.reply(f"Este comando só pode ser usado no canal <#{config.channel_id}>.")
            return

        is_approver = any(
            role.id in config.approver_role_ids for role in ctx.author.roles
        )
        if not ctx.author.guild_permissions.administrator and not is_approver:
            await ctx.reply("Você não tem um cargo autorizado para usar este comando.")
            return

        reference = ctx.message.reference
        if reference is None or reference.message_id is None:
            await ctx.reply("Use `!apv` respondendo à mensagem da ficha.")
            return

        target_message = reference.resolved
        if not isinstance(target_message, discord.Message):
            try:
                target_message = await ctx.channel.fetch_message(reference.message_id)
            except discord.HTTPException:
                await ctx.reply("Não consegui encontrar a mensagem respondida.")
                return

        try:
            approved_role_id, role_keywords = load_role_config(
                config.role_keywords_file
            )
        except (OSError, json.JSONDecodeError, ValueError):
            await ctx.reply(
                f"Não consegui ler '{config.role_keywords_file.name}'. Confira o formato do arquivo."
            )
            return
        if approved_role_id is None:
            await ctx.reply(
                f"Configure 'approved_role_id' em '{config.role_keywords_file.name}'."
            )
            return

        target_text = target_message.content.casefold()
        matched_role_ids = {
            role_id
            for keyword, role_id in role_keywords.items()
            if re.search(
                rf"(?<![^\W_]){re.escape(keyword.casefold())}(?![^\W_])",
                target_text,
            )
        }
        if not matched_role_ids:
            await ctx.reply(
                "Não encontrei uma palavra-chave configurada na mensagem respondida."
            )
            return
        if len(matched_role_ids) > 1:
            await ctx.reply(
                "A mensagem corresponde a mais de um cargo; deixe apenas uma palavra-chave."
            )
            return

        if target_message.author.bot:
            await ctx.reply("A mensagem respondida não foi enviada por um membro.")
            return

        member = ctx.guild.get_member(target_message.author.id)
        if member is None:
            try:
                member = await ctx.guild.fetch_member(target_message.author.id)
            except discord.HTTPException:
                await ctx.reply("Não encontrei o autor da ficha neste servidor.")
                return

        keyword_role_id = matched_role_ids.pop()
        keyword_role = ctx.guild.get_role(keyword_role_id)
        approved_role = ctx.guild.get_role(approved_role_id)
        if keyword_role is None:
            await ctx.reply(f"Não encontrei o cargo com ID `{keyword_role_id}` neste servidor.")
            return
        if approved_role is None:
            await ctx.reply(f"Não encontrei o cargo aprovado com ID `{approved_role_id}` neste servidor.")
            return

        current_role_ids = {role.id for role in member.roles}
        roles_to_add = [
            role
            for role in {keyword_role.id: keyword_role, approved_role.id: approved_role}.values()
            if role.id not in current_role_ids
        ]
        roles_to_remove = [
            role for role in member.roles
            if role.id == config.role_to_remove_id
        ]
        if not roles_to_add and not roles_to_remove:
            await ctx.send(config.approval_message)
            return

        try:
            if roles_to_add:
                await member.add_roles(
                    *roles_to_add,
                    reason=f"Ficha avaliada por {ctx.author}",
                )
            if roles_to_remove:
                await member.remove_roles(
                    *roles_to_remove,
                    reason=f"Cargo atualizado após aprovação por {ctx.author}",
                )
        except discord.Forbidden:
            await ctx.reply(
                "Não consegui atribuir o cargo. Verifique a permissão 'Gerenciar cargos' "
                "e se o cargo do bot está acima do cargo configurado."
            )
            return

        await ctx.send(config.approval_message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ApprovalCog())