import asyncio
import logging

import motor.motor_asyncio
from discord import AutocompleteContext, Embed, AllowedMentions, slash_command, NotFound
from discord.commands import Option
from discord.commands import permissions
from discord.ext import commands

from utils.cfg import cfg
from utils.reporter import report_error

log = logging.getLogger(__name__)
log.setLevel(cfg["log_level"])


class Bridge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo = motor.motor_asyncio.AsyncIOMotorClient(cfg["mongodb_uri"])
        self.db = self.mongo.bridge
        self.bridge_names = []
        self.bridge_queues = {}
        self.bridge_tasks = {}

    @commands.Cog.listener()
    async def on_ready(self):
        await self.maintenance()

    async def maintenance(self):
        all_bridges = await self.db.bridges.distinct("name")
        # create task for each bridge
        # kill all tasks that are not in the bridge_names list
        for bridge in self.bridge_names:
            if bridge not in all_bridges:
                self.bridge_tasks[bridge].cancel()
                del self.bridge_tasks[bridge]
                del self.bridge_queues[bridge]
                self.bridge_names.remove(bridge)
                log.info(f"Bridge {bridge} Task removed!")
        # create task for each bridge that is not in the bridge_tasks list
        for bridge in all_bridges:
            if bridge not in self.bridge_tasks:
                self.bridge_tasks[bridge] = self.bot.loop.create_task(self.bridge_loop(bridge))
                self.bridge_names.append(bridge)
                self.bridge_queues[bridge] = asyncio.Queue()
                log.info(f"Bridge {bridge} Task created!")

    async def bridge_loop(self, bridge_name):
        log.info(f"Bridge {bridge_name} Task started!")
        while True:
            try:
                log.debug(f"Bridge {bridge_name} waiting for messages...")
                payload = await self.bridge_queues[bridge_name].get()
                log.debug(f"Bridge {bridge_name} got new payload!")
                bridge = await self.db.bridges.find_one({"name": bridge_name})
                for channel_id in bridge["channels"]:
                    if channel_id == payload["message"].channel.id:
                        continue
                    match payload["type"]:
                        case "new_message":
                            func = self.handle_new_message
                        case "edited_message":
                            func = self.handle_edited_message
                        case "deleted_message":
                            func = self.handle_deleted_message
                        case _:
                            log.error(f"Bridge {bridge_name} unknown payload type!")
                            continue
                    try:
                        await func(
                            target_channel=channel_id,
                            message=payload["message"],
                            bridge_name=bridge_name
                        )
                    except Exception as e:
                        await report_error(e)
                        log.error(f"Bridge {bridge_name} error: {e}")
                log.debug(f"Bridge {bridge_name} finished handling payload!")
            except asyncio.CancelledError:
                log.info(f"Bridge {bridge_name} stopped!")
                break

    def generate_message_bundle(self, message):
        e = message.embeds[0] if message.embeds else Embed(color=message.author.color)
        e.set_author(name=f"{message.author} (#{message.channel.name} in {message.guild.name})",
                     icon_url=message.author.avatar.url)
        #
        e.description = message.content or "No Message Content"
        # try to display images largely
        if len(message.attachments) == 1 and message.attachments[0].content_type.startswith("image/"):
            a = message.attachments.pop(0)
            e.set_image(url=a.url)
        # otherwise, add attachments as fields
        if message.attachments:
            for i, attachment in enumerate(message.attachments):
                e.add_field(name=f"Attachment #{i + 1}",
                            value=f"[{attachment.description or attachment.filename}]({attachment.url})")
        content = ""
        return content, e

    async def handle_new_message(self, target_channel, message, bridge_name):
        channel = self.bot.get_channel(target_channel)
        if channel is None:
            log.warning(f"Bridge {bridge_name} target channel {target_channel} not found!")
            return
        # handle replies
        reference = None
        if message.reference:
            # check if the message is a reply to a non-bridged message
            db_message = await self.db.messages.find_one(
                {
                    "message_id"    : message.reference.message_id,
                    "target_channel": target_channel
                })
            if db_message:
                # reply to bridged message
                reference = await channel.fetch_message(db_message["bridged_message_id"])
            if not db_message:
                # let's check if this was a reply to a bridged message then
                db_message = await self.db.messages.find_one(
                    {
                        "bridged_message_id": message.reference.message_id
                    })
                if db_message:
                    # good, this is a reply to a bridged message, let's get the original message id
                    original_message_id = db_message["message_id"]
                    # check if the original message is in the target channel
                    try:
                        original_message = await channel.fetch_message(original_message_id)
                    except NotFound:
                        pass
                    else:
                        # good, the original message is in the target channel, let's reply to it
                        reference = original_message
                    if not reference:
                        # check if there is a bridged message in the target channel with this id
                        db_message = await self.db.messages.find_one(
                            {
                                "message_id": original_message_id,
                                "target_channel": target_channel
                            })
                        if db_message:
                            # good, there is a bridged message in the target channel with this id
                            reference = await channel.fetch_message(db_message["bridged_message_id"])
        # handle embeds
        content, e = self.generate_message_bundle(message)

        bridged_message = await channel.send(
            content=content,
            reference=reference,
            embed=e,
            allowed_mentions=AllowedMentions(everyone=False, users=False, roles=False, replied_user=bool(message.mentions))
        )
        # add to message collection
        await self.db.messages.insert_one({
            "message_id"        : message.id,
            "bridged_message_id": bridged_message.id,
            "target_channel"    : target_channel
        })

    async def handle_edited_message(self, target_channel, message, bridge_name):
        channel = self.bot.get_channel(target_channel)
        if channel is None:
            log.warning(f"Bridge {bridge_name} target channel {target_channel} not found!")
            return
        # get bridged message from collection
        bridged_message = await self.db.messages.find_one({"message_id"    : message.id,
                                                           "target_channel": target_channel})
        if bridged_message is None:
            log.warning(f"Bridge {bridge_name} message {message.id} not found!")
            return
        # edit bridged message
        content, e = self.generate_message_bundle(message)
        bridged_message = await channel.fetch_message(bridged_message["bridged_message_id"])
        await bridged_message.edit(content=content, embed=e)

    async def handle_deleted_message(self, target_channel, message, bridge_name):
        channel = self.bot.get_channel(target_channel)
        if channel is None:
            log.warning(f"Bridge {bridge_name} target channel {target_channel} not found!")
            return
        # get bridged message from collection
        bridged_message = await self.db.messages.find_one({"message_id"    : message.id,
                                                           "target_channel": target_channel})
        if bridged_message is None:
            log.warning(f"Bridge {bridge_name} message {message.id} not found!")
            return
        # delete bridged message
        bridged_message = await channel.fetch_message(bridged_message["bridged_message_id"])
        await bridged_message.delete()
        # delete from collection
        await self.db.messages.delete_one({"message_id"    : message.id,
                                           "target_channel": target_channel})

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return
        if message.guild is None:
            return
        # check if this channel is connected to a bridge
        current_channel_id = message.channel.id
        bridge = await self.db.bridges.find_one({"channels": current_channel_id})
        if not bridge:
            return
        await self.bridge_queues[bridge["name"]].put({"type": "new_message", "message": message})
        log.debug(f"Bridge {bridge['name']} put new message in queue!")

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author == self.bot.user:
            return
        if before.guild is None:
            return
        # check if this channel is connected to a bridge
        current_channel_id = before.channel.id
        bridge = await self.db.bridges.find_one({"channels": current_channel_id})
        if not bridge:
            return
        await self.bridge_queues[bridge["name"]].put({"type": "edited_message", "message": after})
        log.debug(f"Bridge {bridge['name']} put edited message in queue!")

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author == self.bot.user:
            return
        if message.guild is None:
            return
        # check if this channel is connected to a bridge
        current_channel_id = message.channel.id
        bridge = await self.db.bridges.find_one({"channels": current_channel_id})
        if not bridge:
            return
        await self.bridge_queues[bridge["name"]].put({"type": "deleted_message", "message": message})

    def match_bridge_names(self, name: AutocompleteContext):
        return [bridge for bridge in self.bridge_names if bridge.startswith(name.value)]

    @slash_command(default_permission=False)
    @permissions.is_owner()
    async def create(self,
                     ctx,
                     bridge_name: str):
        """Create a new bridge"""
        await ctx.defer(ephemeral=True)
        current_channel_id = ctx.channel.id
        # check if this channel is already bridged
        bridge = await self.db.bridges.find_one({"channels": current_channel_id})
        if bridge:
            await ctx.respond(f"This channel is already connected to Bridge {bridge['name']}!", ephemeral=True)
            return

        # check if bridge name is already in use
        bridge = await self.db.bridges.find_one({"name": bridge_name})
        if bridge:
            await ctx.respond("This bridge name is already in use!", ephemeral=True)
            return

        # create the bridge
        await self.db.bridges.insert_one({"name": bridge_name, "channels": [current_channel_id]})
        self.bridge_names.append(bridge_name)
        await self.maintenance()
        await ctx.respond(f"Bridge created!\n"
                          f"Use `/connect {bridge_name}` to connect other channels to this bridge.",
                          ephemeral=True)

    @slash_command(default_permission=False)
    @permissions.is_owner()
    async def delete(self,
                     ctx,
                     bridge_name: Option(
                         str,
                         autocomplete=match_bridge_names
                     )):
        """Delete a bridge"""
        await ctx.defer(ephemeral=True)
        # check if bridge name is already in use
        bridge = await self.db.bridges.find_one({"name": bridge_name})
        if not bridge:
            await ctx.respond("This bridge name is not in use!", ephemeral=True)
            return

        # delete the bridge
        await self.db.bridges.delete_one({"name": bridge_name})
        self.bridge_names.remove(bridge_name)
        await self.maintenance()
        await ctx.respond(f'Bridge {bridge_name} deleted!', ephemeral=True)

    @slash_command(default_permission=False)
    @permissions.is_owner()
    async def connect(self,
                      ctx,
                      bridge_name: Option(
                          str,
                          autocomplete=match_bridge_names
                      )):
        """Connect the current channel to a bridge"""
        await ctx.defer(ephemeral=True)
        # check if bridge name is already in use
        bridge = await self.db.bridges.find_one({"name": bridge_name})
        if not bridge:
            await ctx.respond("This bridge name is not in use!", ephemeral=True)
            return

        # check if this channel is already bridged
        bridge = await self.db.bridges.find_one({"channels": ctx.channel.id})
        if bridge:
            await ctx.respond("This channel is already bridged!", ephemeral=True)
            return

        # connect the channel to the bridge
        await self.db.bridges.update_one({"name": bridge_name},
                                         {"$push": {"channels": ctx.channel.id}})
        await self.maintenance()
        await ctx.respond('Channel connected to bridge!', ephemeral=True)

    async def match_connected_bridge_name(self, ctx: AutocompleteContext):
        bridge = await self.db.bridges.find_one({'channels': ctx.interaction.channel.id})
        if bridge:
            return [bridge['name']]
        return []

    @slash_command(default_permission=False)
    @permissions.is_owner()
    async def disconnect(self,
                         ctx,
                         bridge_name: Option(
                             str,
                             autocomplete=match_connected_bridge_name
                         )):
        """Disconnect the current channel from a bridge"""
        await ctx.defer(ephemeral=True)
        # check if bridge name is already in use
        bridge = await self.db.bridges.find_one({"name": bridge_name})
        if not bridge:
            await ctx.respond("This bridge name is not in use!", ephemeral=True)
            return

        # check if this channel is in the bridge
        bridge = await self.db.bridges.find_one({"channels": ctx.channel.id})
        if not bridge:
            await ctx.respond("This channel is not bridged!", ephemeral=True)
            return

        # disconnect the channel from the bridge
        await self.db.bridges.update_one({"name": bridge_name},
                                         {"$pull": {"channels": ctx.channel.id}})
        await self.maintenance()
        await ctx.respond('Channel disconnected from bridge!', ephemeral=True)


def setup(bot):
    bot.add_cog(Bridge(bot))
