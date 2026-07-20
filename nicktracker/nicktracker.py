import discord
from discord import app_commands
from datetime import datetime
from typing import Optional

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify


class NicknameTracker(commands.Cog):
    """Track nickname changes in your server."""

    # ── Slash command group ──
    nicktrack_group = app_commands.Group(
        name="nicktrack",
        description="Track and view nickname changes",
        guild_only=True,
    )

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=5647382910, force_registration=True)

        default_guild = {
            "tracking_enabled": True,
            "nicknames": {}
        }

        self.config.register_guild(**default_guild)

    # ── Event Listeners ──

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Log nickname changes."""
        if before.guild is None or after.guild is None:
            return
        if before.nick == after.nick:
            return

        guild = after.guild
        tracking = await self.config.guild(guild).tracking_enabled()
        if not tracking:
            return

        await self._log_nickname_change(guild, after.id, before.nick, after.nick)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Log a member's initial nickname when they join."""
        guild = member.guild
        tracking = await self.config.guild(guild).tracking_enabled()
        if not tracking:
            return

        nicknames = await self.config.guild(guild).nicknames()
        user_history = nicknames.get(str(member.id))

        if not user_history:
            await self._log_nickname_change(guild, member.id, None, member.nick, note="joined")
        else:
            last_known = user_history[-1].get("new")
            if last_known != member.nick:
                await self._log_nickname_change(guild, member.id, last_known, member.nick, note="rejoined")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Log when a member leaves."""
        guild = member.guild
        tracking = await self.config.guild(guild).tracking_enabled()
        if not tracking:
            return

        await self._log_nickname_change(guild, member.id, member.nick, None, note="left")

    async def _log_nickname_change(
        self,
        guild: discord.Guild,
        user_id: int,
        old_nick: str | None,
        new_nick: str | None,
        note: str | None = None
    ):
        """Persist a nickname change to config."""
        MAX_HISTORY = 50

        async with self.config.guild(guild).nicknames() as nicknames:
            key = str(user_id)
            history = nicknames.get(key, [])

            entry = {
                "old": old_nick,
                "new": new_nick,
                "changed_at": datetime.utcnow().isoformat()
            }
            if note:
                entry["note"] = note

            history.append(entry)

            if len(history) > MAX_HISTORY:
                history = history[-MAX_HISTORY:]

            nicknames[key] = history

    def _format_nick(self, nick: str | None) -> str:
        """Format a nickname for display, handling None."""
        return nick if nick is not None else "*(no nickname)*"

    async def _build_history_embeds(self, guild: discord.Guild, user: discord.Member, color: int) -> list:
        """Build nickname history embeds for a user."""
        nicknames = await self.config.guild(guild).nicknames()
        history = nicknames.get(str(user.id), [])

        if not history:
            return []

        lines = []
        for entry in history:
            old = self._format_nick(entry.get("old"))
            new = self._format_nick(entry.get("new"))
            changed = datetime.fromisoformat(entry["changed_at"])
            note = entry.get("note")

            if note:
                lines.append(
                    f"**{old}** → **{new}** — {discord.utils.format_dt(changed, 'R')}"
                    f" (`{note}`)"
                )
            else:
                lines.append(
                    f"**{old}** → **{new}** — {discord.utils.format_dt(changed, 'R')}"
                )

        output = "\n".join(reversed(lines))
        embeds = []
        for page in pagify(output, delims=["\n"], page_length=1900):
            embed = discord.Embed(
                title=f"📝 Nickname History: {user.display_name}",
                description=page,
                color=color
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            embeds.append(embed)
        return embeds

    # ── Prefix commands ──

    @commands.guild_only()
    @commands.group(name="nicktrack", aliases=["nt", "nickhistory"], invoke_without_command=True)
    async def nicktrack(self, ctx: commands.Context, user: discord.Member | None = None):
        """View nickname change history for a user.

        **Usage:**
        `[p]nicktrack @User` — Show history for a user
        `[p]nicktrack` — Show your own history
        `[p]nicktrack enable` — Enable tracking
        `[p]nicktrack disable` — Disable tracking
        """
        if user is None:
            user = ctx.author

        embeds = await self._build_history_embeds(ctx.guild, user, await ctx.embed_color())
        if not embeds:
            return await ctx.send(f"📭 No nickname history found for {user.mention}.")
        for embed in embeds:
            await ctx.send(embed=embed)

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @nicktrack.command(name="enable")
    async def nicktrack_enable(self, ctx: commands.Context):
        """Enable nickname tracking in this server."""
        await self.config.guild(ctx.guild).tracking_enabled.set(True)
        await ctx.send("✅ Nickname tracking has been **enabled**.")

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @nicktrack.command(name="disable")
    async def nicktrack_disable(self, ctx: commands.Context):
        """Disable nickname tracking in this server."""
        await self.config.guild(ctx.guild).tracking_enabled.set(False)
        await ctx.send("✅ Nickname tracking has been **disabled**.")

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @nicktrack.command(name="clear")
    async def nicktrack_clear(self, ctx: commands.Context, user: discord.Member):
        """Clear nickname history for a specific user. (Admin only)"""
        async with self.config.guild(ctx.guild).nicknames() as nicknames:
            if str(user.id) in nicknames:
                del nicknames[str(user.id)]
        await ctx.send(f"✅ Cleared nickname history for {user.mention}.")

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @nicktrack.command(name="wipe")
    async def nicktrack_wipe(self, ctx: commands.Context):
        """Wipe ALL nickname history in this server. (Admin only — irreversible!)"""
        await self.config.guild(ctx.guild).nicknames.set({})
        await ctx.send("✅ All nickname history has been wiped.")

    # ── Slash commands (grouped under /nicktrack) ──

    @nicktrack_group.command(name="history", description="View nickname change history for a user")
    @app_commands.describe(user="The user to look up (defaults to yourself)")
    async def slash_nicktrack_history(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        """Slash command: view nickname history."""
        if user is None:
            user = interaction.user

        embeds = await self._build_history_embeds(
            interaction.guild, user, await self.bot.get_embed_color(interaction.guild)
        )
        if not embeds:
            return await interaction.response.send_message(
                f"📭 No nickname history found for {user.mention}.", ephemeral=True
            )
        await interaction.response.send_message(embed=embeds[0])
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed)

    @nicktrack_group.command(name="enable", description="Enable nickname tracking in this server")
    async def slash_nicktrack_enable(self, interaction: discord.Interaction):
        """Slash command: enable tracking."""
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need **Manage Server** permission to do that.", ephemeral=True
            )
        await self.config.guild(interaction.guild).tracking_enabled.set(True)
        await interaction.response.send_message("✅ Nickname tracking has been **enabled**.")

    @nicktrack_group.command(name="disable", description="Disable nickname tracking in this server")
    async def slash_nicktrack_disable(self, interaction: discord.Interaction):
        """Slash command: disable tracking."""
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need **Manage Server** permission to do that.", ephemeral=True
            )
        await self.config.guild(interaction.guild).tracking_enabled.set(False)
        await interaction.response.send_message("✅ Nickname tracking has been **disabled**.")

    @nicktrack_group.command(name="clear", description="Clear nickname history for a user")
    @app_commands.describe(user="The user whose history to clear")
    async def slash_nicktrack_clear(self, interaction: discord.Interaction, user: discord.Member):
        """Slash command: clear one user's history."""
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need **Manage Server** permission to do that.", ephemeral=True
            )
        async with self.config.guild(interaction.guild).nicknames() as nicknames:
            if str(user.id) in nicknames:
                del nicknames[str(user.id)]
        await interaction.response.send_message(f"✅ Cleared nickname history for {user.mention}.")

    @nicktrack_group.command(name="wipe", description="Wipe ALL nickname history in this server")
    async def slash_nicktrack_wipe(self, interaction: discord.Interaction):
        """Slash command: wipe all history."""
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need **Manage Server** permission to do that.", ephemeral=True
            )
        await self.config.guild(interaction.guild).nicknames.set({})
        await interaction.response.send_message("✅ All nickname history has been wiped.")
