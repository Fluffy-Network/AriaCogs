import asyncio
import calendar
import discord
import re
from datetime import datetime, timedelta
from typing import Optional

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify


class ScheduledMessage(commands.Cog):
    """Schedule messages to be sent at a later time."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)

        default_guild = {
            "scheduled_messages": [],
            "timezone": "UTC"
        }

        self.config.register_guild(**default_guild)
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())

    def cog_unload(self):
        self.scheduler_task.cancel()

    async def scheduler_loop(self):
        """Background loop that checks for due messages every 30 seconds."""
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.check_scheduled_messages()
            except Exception as e:
                print(f"[ScheduledMessage] Error in scheduler loop: {e}")
            await asyncio.sleep(30)

    def get_next_occurrence(self, dt: datetime, recurring: str) -> datetime:
        """Calculate the next occurrence of a recurring message."""
        if recurring == "daily":
            return dt + timedelta(days=1)
        elif recurring == "weekly":
            return dt + timedelta(weeks=1)
        elif recurring == "monthly":
            month = dt.month
            year = dt.year
            if month == 12:
                month = 1
                year += 1
            else:
                month += 1
            max_day = calendar.monthrange(year, month)[1]
            day = min(dt.day, max_day)
            return dt.replace(year=year, month=month, day=day)
        return dt

    async def check_scheduled_messages(self):
        """Check all guilds for messages that are due to be sent."""
        now = datetime.utcnow()
        all_guilds = await self.config.all_guilds()

        for guild_id, data in all_guilds.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            messages = data.get("scheduled_messages", [])
            if not messages:
                continue

            updated = False

            for msg in messages:
                scheduled_time = datetime.fromisoformat(msg["time"])

                if scheduled_time <= now:
                    channel = guild.get_channel(msg["channel_id"])
                    if channel and isinstance(channel, discord.TextChannel):
                        perms = channel.permissions_for(guild.me)
                        if perms.send_messages:
                            content = msg["content"]

                            # Extract specific mention IDs from the stored content so Discord
                            # definitely parses them instead of silently stripping them.
                            role_ids = [int(r) for r in re.findall(r"<@&(\d+)>", content)]
                            user_ids = [int(u) for u in re.findall(r"<@!?(\d+)>", content)]
                            everyone = "@everyone" in content or "@here" in content

                            allowed_mentions = discord.AllowedMentions(
                                everyone=everyone and perms.mention_everyone,
                                roles=role_ids or False,
                                users=user_ids or False,
                                replied_user=False,
                            )

                            if msg.get("embed_title"):
                                color = msg.get("embed_color")
                                if color is None:
                                    color = await self.bot.get_embed_color(channel)
                                embed = discord.Embed(
                                    title=msg["embed_title"],
                                    description=content,
                                    color=color
                                )
                                try:
                                    await channel.send(embed=embed, allowed_mentions=allowed_mentions)
                                except discord.HTTPException:
                                    pass
                            else:
                                try:
                                    await channel.send(content, allowed_mentions=allowed_mentions)
                                except discord.HTTPException:
                                    pass

                    if msg.get("recurring"):
                        next_time = self.get_next_occurrence(scheduled_time, msg["recurring"])
                        msg["time"] = next_time.isoformat()
                        updated = True
                    else:
                        msg["_remove"] = True
                        updated = True

            if updated:
                new_messages = [m for m in messages if not m.get("_remove")]
                await self.config.guild(guild).scheduled_messages.set(new_messages)

    def parse_time(self, time_str: str) -> Optional[datetime]:
        """Parse various time formats into a UTC datetime."""
        now = datetime.utcnow()

        # HHMM format (0200)
        if re.match(r"^\d{4}$", time_str):
            hour = int(time_str[:2])
            minute = int(time_str[2:])
            if not (0 <= hour < 24 and 0 <= minute < 60):
                return None
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled <= now:
                scheduled += timedelta(days=1)
            return scheduled

        # HH:MM format (02:00 or 2:00)
        if re.match(r"^\d{1,2}:\d{2}$", time_str):
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                return None
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled <= now:
                scheduled += timedelta(days=1)
            return scheduled

        # YYYY-MM-DD HH:MM
        if re.match(r"^\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}$", time_str):
            try:
                return datetime.strptime(time_str, "%Y-%m-%d %H:%M")
            except ValueError:
                return None

        # MM/DD HH:MM
        if re.match(r"^\d{1,2}/\d{1,2} \d{1,2}:\d{2}$", time_str):
            try:
                dt = datetime.strptime(time_str, "%m/%d %H:%M")
                scheduled = dt.replace(year=now.year)
                if scheduled <= now:
                    scheduled = scheduled.replace(year=now.year + 1)
                return scheduled
            except ValueError:
                return None

        return None

    def parse_flags(self, content: str):
        """Parse flags like --daily, --weekly, --monthly, --title, --color from content."""
        flags = {
            'daily': False,
            'weekly': False,
            'monthly': False,
            'title': None,
            'color': None,
        }
        text = content.strip()

        while True:
            # Match a flag name at the start of the text
            m = re.match(r'^--(\w+)(?:\s+|\b)', text)
            if not m:
                break

            name = m.group(1).lower()
            if name not in flags:
                break

            # Consume the flag name (and trailing whitespace)
            text = text[m.end():].lstrip()

            if name in ('daily', 'weekly', 'monthly'):
                # Boolean flag — no value to consume
                flags[name] = True
            else:
                # Value flag — capture the next token (quoted or unquoted)
                vm = re.match(r'(?:"([^"]*)"|\'([^\']*)\'|([^\s]+))', text)
                if not vm:
                    break
                val = vm.group(1) or vm.group(2) or vm.group(3)
                flags[name] = val
                text = text[vm.end():].lstrip()

        return flags, text.strip()

    def parse_color(self, color_str: Optional[str]) -> Optional[int]:
        """Parse a color string into an integer color value."""
        if not color_str:
            return None

        color_str = color_str.lower().strip()

        named_colors = {
            'red': 0xFF0000,
            'green': 0x00FF00,
            'blue': 0x0000FF,
            'yellow': 0xFFFF00,
            'orange': 0xFFA500,
            'purple': 0x800080,
            'pink': 0xFFC0CB,
            'cyan': 0x00FFFF,
            'white': 0xFFFFFF,
            'black': 0x000000,
            'blurple': 0x5865F2,
            'grey': 0x808080,
            'gray': 0x808080,
            'gold': 0xFFD700,
            'magenta': 0xFF00FF,
            'teal': 0x008080,
            'navy': 0x000080,
        }

        if color_str in named_colors:
            return named_colors[color_str]

        color_str = color_str.lstrip('#')
        if re.match(r'^[0-9a-f]{6}$', color_str):
            return int(color_str, 16)

        if re.match(r'^0x[0-9a-f]{6}$', color_str):
            return int(color_str, 16)

        return None

    def generate_id(self, messages: list) -> int:
        """Generate a unique ID for a new scheduled message."""
        if not messages:
            return 1
        return max(m.get("id", 0) for m in messages) + 1

    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    @commands.group(name="schedule", aliases=["sm", "schedmsg"], invoke_without_command=True)
    async def schedule(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None,
                       time: Optional[str] = None, *, content: Optional[str] = None):
        """Schedule a message to be sent later.

        **Usage:**
        `[p]schedule #channel 0200 Your message here`
        `[p]sm #general 02:00 Hey Early Birds!`
        `[p]schedule #announcements 2024-12-25 14:00 Merry Christmas!`

        **Flags (must come before message content):**
        `--daily` — Repeat every day
        `--weekly` — Repeat every week
        `--monthly` — Repeat every month
        `--title "Title"` — Send as an embed with a title
        `--color blue` — Set embed color (hex or name)

        **Time formats:**
        - `HHMM` (0200 for 2:00 AM)
        - `HH:MM` (02:00 or 2:00)
        - `YYYY-MM-DD HH:MM` (2024-12-25 14:00)
        - `MM/DD HH:MM` (12/25 14:00)

        Times are in **UTC** by default.
        """
        if channel is None or time is None or content is None:
            await ctx.send_help()
            return

        await ctx.invoke(self.schedule_message, channel=channel, time=time, content=content)

    @schedule.command(name="message", aliases=["add", "create"])
    async def schedule_message(self, ctx: commands.Context, channel: discord.TextChannel,
                               time: str, *, content: str):
        """Schedule a message to be sent at a specific time."""
        flags, actual_content = self.parse_flags(content)

        recurring = None
        if flags['daily']:
            recurring = "daily"
        elif flags['weekly']:
            recurring = "weekly"
        elif flags['monthly']:
            recurring = "monthly"

        embed_title = flags['title']
        embed_color = self.parse_color(flags['color'])

        if not actual_content:
            return await ctx.send("You must provide some message content.")

        if len(actual_content) > 2000 and not embed_title:
            return await ctx.send("Message content is too long! Discord has a 2000 character limit.")
        if len(actual_content) > 4096 and embed_title:
            return await ctx.send("Embed description is too long! Maximum is 4096 characters.")

        scheduled_time = self.parse_time(time)
        if not scheduled_time:
            return await ctx.send(
                "Invalid time format. Use `HHMM`, `HH:MM`, `YYYY-MM-DD HH:MM`, or `MM/DD HH:MM`."
            )

        if scheduled_time < datetime.utcnow():
            return await ctx.send("That time is in the past!")

        if not channel.permissions_for(ctx.guild.me).send_messages:
            return await ctx.send(f"I don't have permission to send messages in {channel.mention}.")

        async with self.config.guild(ctx.guild).scheduled_messages() as messages:
            msg_id = self.generate_id(messages)
            messages.append({
                "id": msg_id,
                "channel_id": channel.id,
                "author_id": ctx.author.id,
                "time": scheduled_time.isoformat(),
                "content": actual_content,
                "recurring": recurring,
                "embed_title": embed_title,
                "embed_color": embed_color,
                "created_at": datetime.utcnow().isoformat()
            })

        embed = discord.Embed(
            title="✅ Message Scheduled",
            color=await ctx.embed_color(),
            timestamp=scheduled_time
        )
        embed.add_field(name="ID", value=msg_id, inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        if recurring:
            embed.add_field(name="Recurring", value=recurring.capitalize(), inline=True)
        if embed_title:
            embed.add_field(name="Embed Title", value=embed_title, inline=True)
            if embed_color is not None:
                embed.add_field(name="Embed Color", value=f"#{embed_color:06x}", inline=True)
        embed.add_field(name="Content", value=actual_content[:1024], inline=False)
        embed.set_footer(text="Scheduled for")

        await ctx.send(embed=embed)

    @schedule.command(name="list", aliases=["ls"])
    async def schedule_list(self, ctx: commands.Context):
        """List all scheduled messages for this server."""
        messages = await self.config.guild(ctx.guild).scheduled_messages()

        if not messages:
            return await ctx.send("There are no scheduled messages.")

        msg_lines = []
        for msg in messages:
            channel = ctx.guild.get_channel(msg["channel_id"])
            ch_name = channel.mention if channel else f"Deleted Channel (`{msg['channel_id']}`)"
            author = ctx.guild.get_member(msg["author_id"])
            author_name = author.mention if author else f"Unknown (`{msg['author_id']}`)"
            time = datetime.fromisoformat(msg["time"])

            extra = []
            if msg.get("recurring"):
                extra.append(f"🔁 {msg['recurring'].capitalize()}")
            if msg.get("embed_title"):
                extra.append("📎 Embed")

            msg_lines.append(
                f"**ID:** `{msg['id']}` | **Channel:** {ch_name} | **By:** {author_name}\n"
                f"**Time:** {discord.utils.format_dt(time, 'F')} ({discord.utils.format_dt(time, 'R')})"
                f"{(' | ' + ' | '.join(extra)) if extra else ''}\n"
                f"**Content:** {msg['content'][:100]}{'...' if len(msg['content']) > 100 else ''}"
            )

        output = "\n\n".join(msg_lines)

        for page in pagify(output, delims=["\n\n"], page_length=1900):
            embed = discord.Embed(
                title="📋 Scheduled Messages",
                description=page,
                color=await ctx.embed_color()
            )
            await ctx.send(embed=embed)

    @schedule.command(name="cancel", aliases=["remove", "delete", "rm"])
    async def schedule_cancel(self, ctx: commands.Context, message_id: int):
        """Cancel a scheduled message by its ID."""
        async with self.config.guild(ctx.guild).scheduled_messages() as messages:
            msg = next((m for m in messages if m["id"] == message_id), None)

            if not msg:
                return await ctx.send(f"No scheduled message found with ID `{message_id}`.")

            if msg["author_id"] != ctx.author.id:
                is_admin = await self.bot.is_admin(ctx.author)
                if not is_admin:
                    return await ctx.send("You can only cancel your own scheduled messages.")

            messages.remove(msg)

        await ctx.send(f"✅ Scheduled message `{message_id}` has been cancelled.")

    @schedule.command(name="edit")
    async def schedule_edit(self, ctx: commands.Context, message_id: int,
                            time: Optional[str] = None, *, content: Optional[str] = None):
        """Edit a scheduled message. Provide a new time and/or content.

        **Usage:**
        `[p]schedule edit 5 15:00 New message content`
        `[p]schedule edit 5 New message content` (only edit content)
        `[p]schedule edit 5 15:00` (only edit time)

        You can also use flags in the content:
        `[p]schedule edit 5 --daily --title "New Title" Updated message`
        """
        if not time and not content:
            return await ctx.send("You must provide a new time or content to edit.")

        async with self.config.guild(ctx.guild).scheduled_messages() as messages:
            msg = next((m for m in messages if m["id"] == message_id), None)

            if not msg:
                return await ctx.send(f"No scheduled message found with ID `{message_id}`.")

            if msg["author_id"] != ctx.author.id:
                is_admin = await self.bot.is_admin(ctx.author)
                if not is_admin:
                    return await ctx.send("You can only edit your own scheduled messages.")

            scheduled_time = self.parse_time(time) if time else None

            if scheduled_time and not content:
                if scheduled_time < datetime.utcnow():
                    return await ctx.send("That time is in the past!")
                msg["time"] = scheduled_time.isoformat()

            elif scheduled_time and content:
                if scheduled_time < datetime.utcnow():
                    return await ctx.send("That time is in the past!")
                msg["time"] = scheduled_time.isoformat()

                flags, actual_content = self.parse_flags(content)
                if flags['daily']:
                    msg["recurring"] = "daily"
                elif flags['weekly']:
                    msg["recurring"] = "weekly"
                elif flags['monthly']:
                    msg["recurring"] = "monthly"

                if flags['title'] is not None:
                    msg["embed_title"] = flags['title']
                if flags['color'] is not None:
                    parsed_color = self.parse_color(flags['color'])
                    if parsed_color is not None:
                        msg["embed_color"] = parsed_color

                if actual_content:
                    max_len = 4096 if msg.get("embed_title") else 2000
                    if len(actual_content) > max_len:
                        return await ctx.send(f"Content is too long! Maximum is {max_len} characters.")
                    msg["content"] = actual_content

            else:
                full_content = f"{time} {content}" if time and content else (time or content)
                flags, actual_content = self.parse_flags(full_content)

                if flags['daily']:
                    msg["recurring"] = "daily"
                elif flags['weekly']:
                    msg["recurring"] = "weekly"
                elif flags['monthly']:
                    msg["recurring"] = "monthly"

                if flags['title'] is not None:
                    msg["embed_title"] = flags['title']
                if flags['color'] is not None:
                    parsed_color = self.parse_color(flags['color'])
                    if parsed_color is not None:
                        msg["embed_color"] = parsed_color

                if actual_content:
                    max_len = 4096 if msg.get("embed_title") else 2000
                    if len(actual_content) > max_len:
                        return await ctx.send(f"Content is too long! Maximum is {max_len} characters.")
                    msg["content"] = actual_content

        await ctx.send(f"✅ Scheduled message `{message_id}` has been updated.")

    @schedule.command(name="info", aliases=["details", "show"])
    async def schedule_info(self, ctx: commands.Context, message_id: int):
        """Show details of a specific scheduled message."""
        messages = await self.config.guild(ctx.guild).scheduled_messages()
        msg = next((m for m in messages if m["id"] == message_id), None)

        if not msg:
            return await ctx.send(f"No scheduled message found with ID `{message_id}`.")

        channel = ctx.guild.get_channel(msg["channel_id"])
        author = ctx.guild.get_member(msg["author_id"])
        time = datetime.fromisoformat(msg["time"])
        created = datetime.fromisoformat(msg["created_at"])

        embed = discord.Embed(
            title=f"📨 Scheduled Message #{message_id}",
            color=await ctx.embed_color()
        )
        embed.add_field(name="Channel", value=channel.mention if channel else "Deleted", inline=True)
        embed.add_field(name="Author", value=author.mention if author else "Unknown", inline=True)
        if msg.get("recurring"):
            embed.add_field(name="Recurring", value=msg["recurring"].capitalize(), inline=True)
        if msg.get("embed_title"):
            color_val = msg.get("embed_color")
            embed.add_field(name="Embed Title", value=msg["embed_title"], inline=True)
            embed.add_field(name="Embed Color", value=f"#{color_val:06x}" if color_val else "Default", inline=True)
        embed.add_field(
            name="Scheduled Time",
            value=f"{discord.utils.format_dt(time, 'F')}\n{discord.utils.format_dt(time, 'R')}",
            inline=False
        )
        embed.add_field(name="Content", value=msg["content"][:1024], inline=False)
        embed.set_footer(text=f"Created on {discord.utils.format_dt(created, 'F')}")

        await ctx.send(embed=embed)

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @schedule.group(name="timezone", aliases=["tz"])
    async def schedule_timezone(self, ctx: commands.Context):
        """Configure the timezone for scheduled messages. (Currently informational only — all times are UTC)"""
        await ctx.send_help()

    @schedule_timezone.command(name="set")
    async def timezone_set(self, ctx: commands.Context, timezone: str):
        """Set the server's timezone. (Not yet implemented — defaults to UTC)"""
        await ctx.send("Timezone support is not yet implemented. All times are treated as UTC.")

    @schedule_timezone.command(name="show")
    async def timezone_show(self, ctx: commands.Context):
        """Show the current timezone setting."""
        tz = await self.config.guild(ctx.guild).timezone()
        await ctx.send(f"Current timezone: **{tz}** (All scheduled times should be provided in this timezone.)")
