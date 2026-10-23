import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

intents = discord.Intents.default()
intents.message_content = True

class UtilityBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.load_extension("cogs.apv_fichas")
        await self.load_extension("cogs.fazerlvl")
        await self.load_extension("cogs.help_comandos")
        await self.load_extension("cogs.jogo_apostas_moedas")
        await self.load_extension("cogs.materiais")
        await self.load_extension("cogs.pq")
        await self.load_extension("cogs.minerar")
        await self.load_extension("cogs.minerar_pedras_preciosas")
        await self.load_extension("cogs.expedicoes")
        await self.load_extension("cogs.investimentos")

bot = UtilityBot("!", intents=intents, help_command=None)

@bot.event
async def on_ready():  
    print("Bot iniciado!")

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Defina DISCORD_TOKEN no arquivo .env ou nas variáveis de ambiente.")

bot.run(token)