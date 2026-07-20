import asyncio
import calendar
import discord
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify


class Reminders(commands.Cog):
    """Set personal reminders that DM you or ping you in a channel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1029384756, force_registration=True)

        default_user = {
            "reminders": []
        }

        self.config.register_user(**default_user)
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())

    def cog_unload(self):
        self.scheduler_task.cancel()

    async def scheduler_loop(self):
        """Background loop that checks for due reminders every 15 seconds."""
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.check_reminders()
            except Exception as e:
                print(f"[Reminders] Error in scheduler loop: {e}")
            await asyncio.sleep(15)

    async def check_reminders(self):
        """Check all users for reminders that are due."""
        now = datetime.utcnow()
        all_users = await self.config.all_users()

        for user_id, data in all_users.items():
            user_reminders = data.get("reminders", [])
            if not user_reminders:
                continue

            updated = False

            for reminder in user_reminders:
                remind_time = datetime.fromisoformat(reminder["time"])

                if remind_time <= now:
                    await self._fire_reminder(user_id, reminder)

                    if reminder.get("recurring"):
                        next_time = self._get_next_occurrence(remind_time, reminder["recurring"])
                        reminder["time"] = next_time.isoformat()
                        updated = True
                    else:
                        reminder["_remove"] = True
                        updated = True

            if updated:
                new_reminders = [r for r in user_reminders if not r.get("_remove")]
                user = self.bot.get_user(user_id)
                if user:
                    await self.config.user(user).reminders.set(new_reminders)

    async def _fire_reminder(self, user_id: int, reminder: dict):
        """Send the reminder to the user."""
        user = self.bot.get_user(user_id)
        if not user:
            return

        content = reminder["content"]
        guild_id = reminder.get("guild_id")
        channel_id = reminder.get("channel_id")

        embed = discord.Embed(
            title="⏰ Reminder",
            description=content[:4096],
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Reminder set {discord.utils.format_dt(datetime.fromisoformat(reminder['created_at']), 'R')}")

        # Try DM first if no specific channel
        if channel_id is None:
            try:
                await user.send(embed=embed)
                return
            except discord.Forbidden:
                pass  # DMs closed, try fallback channel
            except discord.HTTPException:
                pass

        # Try the specified channel
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                perms = channel.permissions_for(channel.guild.me)
                if perms.send_messages and perms.embed_links:
                    try:
                        await channel.send(user.mention, embed=embed)
                        return
                    except discord.HTTPException:
                        pass

        # Ultimate fallback: try DM again with plain text
        try:
            await user.send(f"**⏰ Reminder**\n{content}")
        except Exception:
            pass  # Nothing we can do

    def _get_next_occurrence(self, dt: datetime, recurring: str) -> datetime:
        """Calculate the next occurrence of a recurring reminder."""
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

    def _generate_id(self, reminders: list) -> int:
        """Generate a unique ID for a new reminder."""
        if not reminders:
            return 1
        return max(r.get("id", 0) for r in reminders) + 1

    def _parse_time(self, time_str: str) -> Optional[datetime]:
        """Parse various time formats into a UTC datetime."""
        now = datetime.utcnow()
        time_str = time_str.strip().lower()

        # Relative: 2h, 30m, 1d, 1w
        rel_match = re.match(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*w)?$", time_str)
        if rel_match:
            h, m, d, w = rel_match.groups()
            if any((h, m, d, w)):
                delta = timedelta(
                    hours=int(h) if h else 0,
                    minutes=int(m) if m else 0,
                    days=int(d) if d else 0,
                    weeks=int(w) if w else 0
                )
                if delta.total_seconds() > 0:
                    return now + delta

        # Single token relative: just "30" → assume minutes
        if re.match(r"^\d+$", time_str):
            return now + timedelta(minutes=int(time_str))

        # Relative with keyword: "2 hours", "30 minutes", "1 day", "1 week"
        kw_match = re.match(r"^(\d+)\s*(hour|hours|hr|hrs|minute|minutes|min|mins|day|days|week|weeks)$", time_str)
        if kw_match:
            num, unit = int(kw_match.group(1)), kw_match.group(2)
            if unit.startswith("h"):
                return now + timedelta(hours=num)
            elif unit.startswith("m"):
                return now + timedelta(minutes=num)
            elif unit.startswith("d"):
                return now + timedelta(days=num)
            elif unit.startswith("w"):
                return now + timedelta(weeks=num)

        # HH:MM or H:MM
        hm_match = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$", time_str)
        if hm_match:
            hour, minute, ampm = int(hm_match.group(1)), int(hm_match.group(2)), hm_match.group(3)
            if ampm:
                if ampm.lower() == "pm" and hour != 12:
                    hour += 12
                elif ampm.lower() == "am" and hour == 12:
                    hour = 0
            if 0 <= hour < 24 and 0 <= minute < 60:
                scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if scheduled <= now:
                    scheduled += timedelta(days=1)
                return scheduled

        # YYYY-MM-DD HH:MM
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}$", time_str):
            try:
                return datetime.strptime(time_str, "%Y-%m-%d %H:%M")
            except ValueError:
                pass

        # MM/DD HH:MM
        if re.match(r"^\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}$", time_str):
            try:
                dt = datetime.strptime(time_str, "%m/%d %H:%M")
                scheduled = dt.replace(year=now.year)
                if scheduled <= now:
                    scheduled = scheduled.replace(year=now.year + 1)
                return scheduled
            except ValueError:
                pass

        return None

    def _parse_input(self, args: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        Parse reminder input.
        Returns: (recurring, time_str, content, error)
        """
        args = args.strip()
        if not args:
            return None, None, None, "You must provide a time and message."

        tokens = args.split()

        # Check for recurring keyword at the start
        recurring = None
        if tokens[0].lower() in ("daily", "weekly", "monthly"):
            recurring = tokens[0].lower()
            tokens = tokens[1:]

        if not tokens:
            return None, None, None, "You must provide a time and message."

        # Try single-token time
        time_str = tokens[0]
        parsed = self._parse_time(time_str)

        if parsed:
            content = " ".join(tokens[1:])
            if not content:
                return None, None, None, "You must provide a reminder message."
            return recurring, time_str, content, None

        # Try two-token time (date + time)
        if len(tokens) >= 2:
            time_str = f"{tokens[0]} {tokens[1]}"
            parsed = self._parse_time(time_str)
            if parsed:
                content = " ".join(tokens[2:])
                if not content:
                    return None, None, None, "You must provide a reminder message."
                return recurring, time_str, content, None

        return None, None, None, f"Invalid time format: `{tokens[0]}`. Try `2h`, `30m`, `14:00`, or `2026-12-25 09:00`."

    @commands.command(name="remind")
    async def remind(self, ctx: commands.Context, *, args: str):
        """Set a personal reminder.

        **Usage:**
        `[p]remind 2h Check the oven`
        `[p]remind 30m Take a break`
        `[p]remind 14:00 Meeting time`
        `[p]remind 2026-12-25 09:00 Open presents!`
        `[p]remind daily 09:00 Drink water`
        `[p]remind weekly 14:00 Weekly report`

        **Time formats:**
        - `2h`, `30m`, `1d`, `1w` — relative time
        - `2 hours`, `30 minutes` — natural relative
        - `14:00`, `2:00pm` — absolute time today/tomorrow
        - `2026-12-25 14:00` — specific date & time
        - `12/25 14:00` — month/day format

        Add `daily`, `weekly`, or `monthly` at the start for recurring reminders.
        """
        recurring, time_str, content, error = self._parse_input(args)

        if error:
            return await ctx.send(f"❌ {error}")

        scheduled_time = self._parse_time(time_str)
        if not scheduled_time:
            return await ctx.send(f"❌ Could not parse time: `{time_str}`.")

        if scheduled_time < datetime.utcnow():
            return await ctx.send("❌ That time is in the past!")

        async with self.config.user(ctx.author).reminders() as reminders:
            if len(reminders) >= 20:
                return await ctx.send("❌ You can only have 20 reminders at a time. Cancel one first with `[p]remindcancel <id>`.")

            reminder_id = self._generate_id(reminders)
            reminders.append({
                "id": reminder_id,
                "content": content,
                "time": scheduled_time.isoformat(),
                "recurring": recurring,
                "guild_id": ctx.guild.id if ctx.guild else None,
                "channel_id": ctx.channel.id if ctx.guild else None,
                "created_at": datetime.utcnow().isoformat()
            })

        embed = discord.Embed(
            title="⏰ Reminder Set",
            color=await ctx.embed_color(),
            timestamp=scheduled_time
        )
        embed.add_field(name="ID", value=reminder_id, inline=True)
        embed.add_field(name="When", value=f"{discord.utils.format_dt(scheduled_time, 'F')}\n{discord.utils.format_dt(scheduled_time, 'R')}", inline=False)
        if recurring:
            embed.add_field(name="Recurring", value=recurring.capitalize(), inline=True)
        embed.add_field(name="Message", value=content[:1024], inline=False)

        if ctx.guild:
            embed.set_footer(text="I'll remind you here. Use `[p]reminders` to manage your reminders.")
        else:
            embed.set_footer(text="I'll remind you in DMs. Use `[p]reminders` to manage your reminders.")

        await ctx.send(embed=embed)

    @commands.command(name="reminders", aliases=["myreminders", "remindlist"])
    async def reminders(self, ctx: commands.Context):
        """List all your active reminders."""
        reminders = await self.config.user(ctx.author).reminders()

        if not reminders:
            return await ctx.send("📭 You have no active reminders.")

        lines = []
        for r in reminders:
            time = datetime.fromisoformat(r["time"])
            extra = []
            if r.get("recurring"):
                extra.append(f"🔁 {r['recurring'].capitalize()}")
            if r.get("channel_id"):
                ch = self.bot.get_channel(r["channel_id"])
                extra.append(f"📍 {ch.mention if ch else 'Unknown'}")
            else:
                extra.append("📬 DM")

            lines.append(
                f"**ID:** `{r['id']}` | {' | '.join(extra)}\n"
                f"**When:** {discord.utils.format_dt(time, 'F')} ({discord.utils.format_dt(time, 'R')})\n"
                f"**Message:** {r['content'][:100]}{'...' if len(r['content']) > 100 else ''}"
            )

        output = "\n\n".join(lines)

        for page in pagify(output, delims=["\n\n"], page_length=1900):
            embed = discord.Embed(
                title="⏰ Your Reminders",
                description=page,
                color=await ctx.embed_color()
            )
            await ctx.send(embed=embed)

    @commands.command(name="remindcancel", aliases=["cancelreminder", "reminddel"])
    async def remindcancel(self, ctx: commands.Context, reminder_id: int):
        """Cancel a reminder by its ID.

        **Usage:**
        `[p]remindcancel 3`
        """
        async with self.config.user(ctx.author).reminders() as reminders:
            reminder = next((r for r in reminders if r["id"] == reminder_id), None)
            if not reminder:
                return await ctx.send(f"❌ No reminder found with ID `{reminder_id}`.")

            reminders.remove(reminder)

        await ctx.send(f"✅ Reminder `{reminder_id}` has been cancelled.")

    @commands.command(name="remindinfo")
    async def remindinfo(self, ctx: commands.Context, reminder_id: int):
        """Show details of a specific reminder."""
        reminders = await self.config.user(ctx.author).reminders()
        reminder = next((r for r in reminders if r["id"] == reminder_id), None)

        if not reminder:
            return await ctx.send(f"❌ No reminder found with ID `{reminder_id}`.")

        time = datetime.fromisoformat(reminder["time"])
        created = datetime.fromisoformat(reminder["created_at"])

        embed = discord.Embed(
            title=f"⏰ Reminder #{reminder_id}",
            color=await ctx.embed_color(),
            timestamp=time
        )
        embed.add_field(name="Message", value=reminder["content"][:1024], inline=False)
        embed.add_field(name="When", value=f"{discord.utils.format_dt(time, 'F')}\n{discord.utils.format_dt(time, 'R')}", inline=False)
        if reminder.get("recurring"):
            embed.add_field(name="Recurring", value=reminder["recurring"].capitalize(), inline=True)
        if reminder.get("channel_id"):
            ch = self.bot.get_channel(reminder["channel_id"])
            embed.add_field(name="Location", value=ch.mention if ch else "Unknown channel", inline=True)
        else:
            embed.add_field(name="Location", value="DM", inline=True)
        embed.set_footer(text=f"Created {discord.utils.format_dt(created, 'R')}")

        await ctx.send(embed=embed)
