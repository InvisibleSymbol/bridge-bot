import logging

import motor.motor_asyncio
from discord.ext import commands, tasks
from discord.ext.commands import Context
import datetime
from discord import app_commands


from utils.cfg import cfg
from utils.reporter import report_error

log = logging.getLogger(__name__)
log.setLevel(cfg["log_level"])


class DMBlocker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo = motor.motor_asyncio.AsyncIOMotorClient(cfg["mongodb_uri"])
        self.db = self.mongo.dm_blocker
        if not self.run_loop.is_running() and bot.is_ready():
            self.run_loop.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.run_loop.is_running():
            return
        self.run_loop.start()


    @tasks.loop(hours=3) # hourly
    async def run_loop(self):
        # get all pinned messages in db
        enabled_servers = await self.db.guilds.find().to_list(length=None)
        for server in enabled_servers:
            try:
                guild = await self.bot.fetch_guild(server["id"])
                await guild.edit(
                    dms_disabled_until=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24))
            except Exception as err:
                await report_error(err)

# only allow people with admin perms to enable/disable dm protection
    @commands.hybrid_command(default_permission=False)
    @commands.has_permissions(administrator=True)
    async def enable_dm_protection(self, ctx: Context):
        await ctx.defer(ephemeral=True)
        # try enabling it to check if the bot has the correct perms
        # if yes then we will add it to the db so it constantly gets refreshed
        # if not tell the user that the bot needs the manage guild / manage server perms
        try:
            await ctx.guild.edit(
                dms_disabled_until=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24))
            await self.db.guilds.insert_one({"id": ctx.guild.id})
            await ctx.reply("Enabled dm protection", ephemeral=True)
        except Exception as err:
            await ctx.reply("I need the manage server permission to enable dm protection", ephemeral=True)
            await report_error(err)


    @commands.hybrid_command(default_permission=False)
    @commands.has_permissions(administrator=True)
    async def disable_dm_protection(self, ctx: Context):
        await ctx.defer(ephemeral=True)
        try:
            await ctx.guild.edit(dms_disabled_until=None)
            await self.db.guilds.delete_one({"id": ctx.guild.id})
            await ctx.reply("Disabled dm protection", ephemeral=True)
        except Exception as err:
            await ctx.reply("I need the manage server permission to disable dm protection", ephemeral=True)
            await report_error(err)



async def setup(bot):
    await bot.add_cog(DMBlocker(bot))
