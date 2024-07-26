import logging
import random
from pathlib import Path

import discord.errors
from discord.ext.commands import Bot

from utils import reporter
from utils.cfg import cfg

logging.basicConfig(format="%(levelname)5s %(asctime)s [%(name)s] %(filename)s:%(lineno)d|%(funcName)s(): %(message)s")
log = logging.getLogger("discord_bot")
log.setLevel(cfg["log_level"])
logging.getLogger().setLevel("INFO")

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guild_messages = True

log.info('Loading Plugins')
class BridgeBot(Bot):
    async def setup_hook(self):
        for path in Path("plugins").glob('**/*.py'):
            plugin_name = path.parts[1]
            if path.stem != plugin_name:
                log.warning(f"Skipping plugin {plugin_name}")
                continue
            extension_name = f"plugins.{plugin_name}.{plugin_name}"
            log.debug(f"Loading Plugin \"{extension_name}\"")
            try:
                await bot.load_extension(extension_name)
            except Exception as err:
                log.error(f"Failed to load plugin \"{extension_name}\"")
                log.exception(err)

log.info('Finished loading Plugins')

log.info('Starting bot')
bot = BridgeBot(command_prefix=str(random.random()), intents=intents)
reporter.bot = bot
bot.run(cfg["discord.secret"])
