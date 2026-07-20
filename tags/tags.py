import discord
from discord import app_commands
from datetime import datetime

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify


class Tags(commands.Cog):
    """Create and manage custom command tags for your server."""

    # ── Slash command group ──
    tag_group = app_commands.Group(
        name="tag",
        description="Manage custom server tags",
        guild_only=True,
    )

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1928374650, force_registration=True)

        default_guild = {
            "tags": {}
        }

        self.config.register_guild(**default_guild)

    # ── Helpers ──

    def _substitute_variables_ctx(self, content: str, ctx: commands.Context) -> str:
        """Replace template variables using a prefix command context."""
        now = datetime.utcnow()
        replacements = {
            "{user}": ctx.author.mention,
            "{author}": ctx.author.mention,
            "{username}": ctx.author.display_name,
            "{guild}": ctx.guild.name if ctx.guild else "Unknown Server",
            "{channel}": ctx.channel.mention if isinstance(ctx.channel, discord.TextChannel) else str(ctx.channel),
            "{date}": now.strftime("%Y-%m-%d"),
            "{time}": now.strftime("%H:%M UTC"),
        }
        for key, val in replacements.items():
            content = content.replace(key, val)
        return content

    def _substitute_variables_interaction(self, content: str, interaction: discord.Interaction) -> str:
        """Replace template variables using a slash command interaction."""
        now = datetime.utcnow()
        user = interaction.user
        guild = interaction.guild
        channel = interaction.channel

        replacements = {
            "{user}": user.mention,
            "{author}": user.mention,
            "{username}": user.display_name,
            "{guild}": guild.name if guild else "Unknown Server",
            "{channel}": channel.mention if isinstance(channel, discord.TextChannel) else str(channel),
            "{date}": now.strftime("%Y-%m-%d"),
            "{time}": now.strftime("%H:%M UTC"),
        }
        for key, val in replacements.items():
            content = content.replace(key, val)
        return content

    # ── Prefix commands ──

    @commands.guild_only()
    @commands.group(name="tag", aliases=["tags", "t"], invoke_without_command=True)
    async def tag(self, ctx: commands.Context, name: str | None = None):
        """View or manage custom tags.

        **Usage:**
        `[p]tag <name>` — Run a tag
        `[p]tag add <name> <content>` — Create a tag
        `[p]tag remove <name>` — Delete a tag
        `[p]tag list` — List all tags
        `[p]tag info <name>` — Show tag details
        `[p]tag edit <name> <new content>` — Edit a tag
        `[p]tag raw <name>` — Show raw tag content

        **Variables:**
        `{user}` `{author}` `{username}` `{guild}` `{channel}` `{date}` `{time}`
        """
        if name is None:
            await ctx.send_help()
            return

        tags = await self.config.guild(ctx.guild).tags()
        tag_data = tags.get(name.lower())

        if not tag_data:
            if name.lower() in ("add", "remove", "delete", "list", "info", "edit", "raw"):
                await ctx.send_help()
                return
            return await ctx.send(f"❌ Tag `{name}` doesn't exist.")

        async with self.config.guild(ctx.guild).tags() as tags:
            tags[name.lower()]["uses"] = tags[name.lower()].get("uses", 0) + 1

        content = self._substitute_variables_ctx(tag_data["content"], ctx)
        await ctx.send(content)

    @tag.command(name="add", aliases=["create"])
    async def tag_add(self, ctx: commands.Context, name: str, *, content: str):
        """Create a new tag."""
        result = await self._create_tag(ctx.guild, ctx.author, name, content)
        await ctx.send(result)

    @tag.command(name="remove", aliases=["delete", "rm"])
    async def tag_remove(self, ctx: commands.Context, name: str):
        """Delete a tag. Only the tag author or a moderator can delete it."""
        result = await self._delete_tag(ctx.guild, ctx.author, name)
        await ctx.send(result)

    @tag.command(name="list", aliases=["ls"])
    async def tag_list(self, ctx: commands.Context):
        """List all tags in this server."""
        embeds = await self._build_tag_list_embeds(ctx.guild, await ctx.embed_color())
        if not embeds:
            return await ctx.send("There are no tags in this server.")
        for embed in embeds:
            await ctx.send(embed=embed)

    @tag.command(name="info", aliases=["details"])
    async def tag_info(self, ctx: commands.Context, name: str):
        """Show detailed info about a tag."""
        embed = await self._build_tag_info_embed(ctx.guild, name, await ctx.embed_color())
        if embed:
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Tag `{name}` doesn't exist.")

    @tag.command(name="edit")
    async def tag_edit(self, ctx: commands.Context, name: str, *, new_content: str):
        """Edit an existing tag."""
        result = await self._edit_tag(ctx.guild, ctx.author, name, new_content)
        await ctx.send(result)

    @tag.command(name="raw")
    async def tag_raw(self, ctx: commands.Context, name: str):
        """Show the raw content of a tag (without variable substitution)."""
        embed = await self._build_tag_raw_embed(ctx.guild, name, await ctx.embed_color())
        if embed:
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Tag `{name}` doesn't exist.")

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @tag.command(name="purge")
    async def tag_purge(self, ctx: commands.Context, user: discord.Member):
        """Delete all tags created by a specific user. (Admin only)"""
        async with self.config.guild(ctx.guild).tags() as tags:
            to_remove = [name for name, data in tags.items() if data["author_id"] == user.id]
            for name in to_remove:
                del tags[name]
        await ctx.send(f"✅ Deleted {len(to_remove)} tag(s) created by {user.mention}.")

    # ── Shared helpers ──

    async def _create_tag(self, guild: discord.Guild, author: discord.Member, name: str, content: str) -> str:
        if len(name) > 50:
            return "❌ Tag name is too long! Maximum is 50 characters."
        if len(content) > 2000:
            return "❌ Tag content is too long! Maximum is 2000 characters."
        if name.lower() in ("add", "remove", "delete", "list", "info", "edit", "raw", "run", "purge"):
            return "❌ That name is reserved."

        async with self.config.guild(guild).tags() as tags:
            if name.lower() in tags:
                return f"❌ Tag `{name}` already exists. Use `/tag edit` or `tag edit` to modify it."
            tags[name.lower()] = {
                "content": content,
                "author_id": author.id,
                "uses": 0,
                "created_at": datetime.utcnow().isoformat(),
                "modified_at": datetime.utcnow().isoformat()
            }
        return f"✅ Tag `{name}` has been created."

    async def _delete_tag(self, guild: discord.Guild, author: discord.Member, name: str) -> str:
        async with self.config.guild(guild).tags() as tags:
            tag_data = tags.get(name.lower())
            if not tag_data:
                return f"❌ Tag `{name}` doesn't exist."

            is_admin = await self.bot.is_admin(author)
            if tag_data["author_id"] != author.id and not is_admin:
                return "You can only delete your own tags."

            del tags[name.lower()]
        return f"✅ Tag `{name}` has been deleted."

    async def _edit_tag(self, guild: discord.Guild, author: discord.Member, name: str, new_content: str) -> str:
        if len(new_content) > 2000:
            return "❌ Tag content is too long! Maximum is 2000 characters."

        async with self.config.guild(guild).tags() as tags:
            tag_data = tags.get(name.lower())
            if not tag_data:
                return f"❌ Tag `{name}` doesn't exist."

            is_admin = await self.bot.is_admin(author)
            if tag_data["author_id"] != author.id and not is_admin:
                return "You can only edit your own tags."

            tag_data["content"] = new_content
            tag_data["modified_at"] = datetime.utcnow().isoformat()
            tags[name.lower()] = tag_data
        return f"✅ Tag `{name}` has been updated."

    async def _build_tag_list_embeds(self, guild: discord.Guild, color: int) -> list:
        tags = await self.config.guild(guild).tags()
        if not tags:
            return []

        lines = []
        for name, data in sorted(tags.items(), key=lambda x: x[0]):
            author = guild.get_member(data["author_id"])
            author_name = author.mention if author else f"Unknown ({data['author_id']})"
            uses = data.get("uses", 0)
            lines.append(f"`{name}` — by {author_name} — used {uses} time{'s' if uses != 1 else ''}")

        output = "\n".join(lines)
        embeds = []
        for page in pagify(output, delims=["\n"], page_length=1900):
            embeds.append(discord.Embed(title="🏷️ Server Tags", description=page, color=color))
        return embeds

    async def _build_tag_info_embed(self, guild: discord.Guild, name: str, color: int) -> discord.Embed | None:
        tags = await self.config.guild(guild).tags()
        tag_data = tags.get(name.lower())
        if not tag_data:
            return None

        author = guild.get_member(tag_data["author_id"])
        created = datetime.fromisoformat(tag_data["created_at"])
        modified = datetime.fromisoformat(tag_data["modified_at"])
        uses = tag_data.get("uses", 0)

        embed = discord.Embed(title=f"🏷️ Tag: {name}", color=color)
        embed.add_field(name="Author", value=author.mention if author else "Unknown", inline=True)
        embed.add_field(name="Uses", value=str(uses), inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(created, "F"), inline=False)
        embed.add_field(name="Modified", value=discord.utils.format_dt(modified, "F"), inline=False)
        embed.add_field(name="Content", value=tag_data["content"][:1024], inline=False)
        return embed

    async def _build_tag_raw_embed(self, guild: discord.Guild, name: str, color: int) -> discord.Embed | None:
        tags = await self.config.guild(guild).tags()
        tag_data = tags.get(name.lower())
        if not tag_data:
            return None

        content = tag_data["content"]
        content = content.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
        return discord.Embed(
            title=f"🏷️ Raw Content: {name}",
            description=f"```\n{content[:1990]}\n```",
            color=color
        )

    # ── Slash commands (grouped under /tag) ──

    @tag_group.command(name="run", description="Run a tag")
    @app_commands.describe(name="The name of the tag to run")
    async def slash_tag_run(self, interaction: discord.Interaction, name: str):
        """Slash command: run a tag."""
        tags = await self.config.guild(interaction.guild).tags()
        tag_data = tags.get(name.lower())

        if not tag_data:
            return await interaction.response.send_message(
                f"❌ Tag `{name}` doesn't exist.", ephemeral=True
            )

        async with self.config.guild(interaction.guild).tags() as tags:
            tags[name.lower()]["uses"] = tags[name.lower()].get("uses", 0) + 1

        content = self._substitute_variables_interaction(tag_data["content"], interaction)
        await interaction.response.send_message(content)

    @tag_group.command(name="add", description="Create a new tag")
    @app_commands.describe(name="The tag name", content="The tag content")
    async def slash_tag_add(self, interaction: discord.Interaction, name: str, content: str):
        """Slash command: create a tag."""
        result = await self._create_tag(interaction.guild, interaction.user, name, content)
        await interaction.response.send_message(result, ephemeral=result.startswith("❌"))

    @tag_group.command(name="remove", description="Delete a tag")
    @app_commands.describe(name="The tag name to delete")
    async def slash_tag_remove(self, interaction: discord.Interaction, name: str):
        """Slash command: delete a tag."""
        result = await self._delete_tag(interaction.guild, interaction.user, name)
        await interaction.response.send_message(result, ephemeral=result.startswith("❌"))

    @tag_group.command(name="list", description="List all tags in this server")
    async def slash_tag_list(self, interaction: discord.Interaction):
        """Slash command: list tags."""
        embeds = await self._build_tag_list_embeds(interaction.guild, await self.bot.get_embed_color(interaction.guild))
        if not embeds:
            return await interaction.response.send_message("There are no tags in this server.", ephemeral=True)
        await interaction.response.send_message(embed=embeds[0])
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed)

    @tag_group.command(name="info", description="Show info about a tag")
    @app_commands.describe(name="The tag name")
    async def slash_tag_info(self, interaction: discord.Interaction, name: str):
        """Slash command: tag info."""
        embed = await self._build_tag_info_embed(interaction.guild, name, await self.bot.get_embed_color(interaction.guild))
        if embed:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(f"❌ Tag `{name}` doesn't exist.", ephemeral=True)

    @tag_group.command(name="edit", description="Edit an existing tag")
    @app_commands.describe(name="The tag name", new_content="The new content")
    async def slash_tag_edit(self, interaction: discord.Interaction, name: str, new_content: str):
        """Slash command: edit a tag."""
        result = await self._edit_tag(interaction.guild, interaction.user, name, new_content)
        await interaction.response.send_message(result, ephemeral=result.startswith("❌"))

    @tag_group.command(name="raw", description="Show raw tag content")
    @app_commands.describe(name="The tag name")
    async def slash_tag_raw(self, interaction: discord.Interaction, name: str):
        """Slash command: raw tag content."""
        embed = await self._build_tag_raw_embed(interaction.guild, name, await self.bot.get_embed_color(interaction.guild))
        if embed:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(f"❌ Tag `{name}` doesn't exist.", ephemeral=True)
