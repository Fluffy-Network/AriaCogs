import asyncio
import aiohttp
import discord
from discord import app_commands
import re

from redbot.core import commands, Config
from redbot.core.bot import Red


class ArchiveBox(commands.Cog):
    """Submit URLs to ArchiveBox for archiving."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9876543210, force_registration=True)

        default_global = {
            "base_url": "https://archive.fluffynet.dev"
        }

        self.config.register_global(**default_global)

    # ── Helpers ──

    async def _get_api_key(self) -> str | None:
        """Retrieve the ArchiveBox API key from Red's shared API tokens."""
        tokens = await self.bot.get_shared_api_tokens("archivebox")
        return tokens.get("api_key")

    def _get_headers(self, api_key: str) -> dict:
        """Build the Authorization headers for ArchiveBox API requests."""
        return {
            "X-ArchiveBox-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _normalize_url(self, url: str) -> str:
        """Strip Discord angle brackets and whitespace from URLs."""
        return url.strip("<> \t\n")

    def _is_valid_url(self, url: str) -> bool:
        """Basic URL validation."""
        return bool(re.match(r"^https?://", url, re.IGNORECASE))

    def _urls_match(self, url1: str, url2: str) -> bool:
        """Compare two URLs loosely (strip trailing slashes)."""
        return url1.rstrip("/") == url2.rstrip("/")

    def _status_ok(self, status: str | None) -> bool:
        """Check if an ArchiveBox status means the snapshot is complete."""
        return status in ("sealed", "succeeded", "verified")

    def _format_tags(self, tags) -> str:
        """Normalise tags (list or string) to a comma-separated string."""
        if isinstance(tags, list):
            return ", ".join(str(t) for t in tags)
        return str(tags) if tags else ""

    def _build_archive_link(self, base_url: str, snapshot: dict) -> str:
        """Build a browsable archive link from a snapshot dict."""
        timestamp = snapshot.get("timestamp")
        if timestamp:
            return f"{base_url.rstrip('/')}/archive/{timestamp}"
        archive_path = snapshot.get("archive_path")
        if archive_path:
            return f"{base_url.rstrip('/')}/{archive_path}"
        return base_url

    def _build_success_embed(
        self,
        snapshot: dict,
        base_url: str,
        original_url: str,
        *,
        already_archived: bool = False
    ) -> discord.Embed:
        """Build the embed shown when a snapshot is ready."""
        archive_link = self._build_archive_link(base_url, snapshot)
        title = snapshot.get("title")
        archived_url = snapshot.get("url", original_url)
        status = snapshot.get("status", "unknown")
        tags = self._format_tags(snapshot.get("tags"))

        color = discord.Color.green() if self._status_ok(status) else discord.Color.blurple()
        header = "📦 Already Archived" if already_archived else "📦 URL Archived"
        desc = (
            "This URL was already in ArchiveBox — no duplicate was created."
            if already_archived else None
        )

        embed = discord.Embed(
            title=header,
            description=desc,
            url=archive_link,
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Original URL", value=archived_url[:1024], inline=False)
        embed.add_field(name="Archive Link", value=f"[View Snapshot]({archive_link})", inline=False)
        embed.add_field(name="Title", value=title[:1024] if title else "N/A", inline=False)
        if tags:
            embed.add_field(name="Tags", value=tags[:1024], inline=False)
        embed.set_footer(text=f"Status: {status.capitalize()}")
        return embed

    def _extract_items(self, data) -> list:
        """Extract the items list from an ArchiveBox paginated response."""
        if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
            return data["items"]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "result", "data", "snapshots"):
                if key in data:
                    val = data[key]
                    if isinstance(val, list):
                        return val
                    if isinstance(val, dict):
                        return [val]
            return [data]
        return []

    async def _find_snapshot(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str,
        target_url: str
    ) -> dict | None:
        """Search ArchiveBox for an existing, complete snapshot of target_url."""
        endpoint = f"{base_url.rstrip('/')}/api/v1/core/snapshots"
        headers = self._get_headers(api_key)

        try:
            async with session.get(
                endpoint,
                headers=headers,
                params={
                    "url": target_url,
                    "with_archiveresults": "false",
                    "limit": 200,
                    "offset": 0,
                    "page": 0
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for snapshot in self._extract_items(data):
                        if isinstance(snapshot, dict):
                            snap_url = snapshot.get("url", "")
                            if self._urls_match(snap_url, target_url):
                                status = snapshot.get("status", "")
                                if self._status_ok(status) and snapshot.get("timestamp"):
                                    return snapshot
        except Exception:
            pass

        return None

    async def _poll_for_snapshot(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str,
        target_url: str,
        pending_msg: discord.Message,
        max_wait: int = 180,
        interval: int = 5
    ) -> dict | None:
        """Poll ArchiveBox until the snapshot is ready or we time out."""
        start_time = discord.utils.utcnow()

        while (discord.utils.utcnow() - start_time).total_seconds() < max_wait:
            await asyncio.sleep(interval)

            snapshot = await self._find_snapshot(session, api_key, base_url, target_url)
            if snapshot and snapshot.get("timestamp"):
                return snapshot

            elapsed = int((discord.utils.utcnow() - start_time).total_seconds())
            try:
                embed = discord.Embed(
                    title="📦 Archiving...",
                    description=(
                        f"Submitting `{target_url}` to ArchiveBox...\n"
                        f"⏳ Waiting for snapshot to be generated... ({elapsed}s)"
                    ),
                    color=discord.Color.orange()
                )
                await pending_msg.edit(embed=embed)
            except Exception:
                pass

        return None

    async def _slash_poll_for_snapshot(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str,
        target_url: str,
        interaction: discord.Interaction,
        max_wait: int = 180,
        interval: int = 5
    ) -> dict | None:
        """Poll ArchiveBox until the snapshot is ready, editing the deferred interaction."""
        start_time = discord.utils.utcnow()

        while (discord.utils.utcnow() - start_time).total_seconds() < max_wait:
            await asyncio.sleep(interval)

            snapshot = await self._find_snapshot(session, api_key, base_url, target_url)
            if snapshot and snapshot.get("timestamp"):
                return snapshot

            elapsed = int((discord.utils.utcnow() - start_time).total_seconds())
            try:
                embed = discord.Embed(
                    title="📦 Archiving...",
                    description=(
                        f"Submitting `{target_url}` to ArchiveBox...\n"
                        f"⏳ Waiting for snapshot to be generated... ({elapsed}s)"
                    ),
                    color=discord.Color.orange()
                )
                await interaction.edit_original_response(embed=embed)
            except Exception:
                pass

        return None

    # ── Prefix commands ──

    @commands.command(name="archive")
    @commands.mod_or_permissions(manage_messages=True)
    async def archive(self, ctx: commands.Context, url: str):
        """Submit a URL to ArchiveBox for archiving."""
        api_key = await self._get_api_key()
        base_url = await self.config.base_url()

        if not api_key:
            embed = discord.Embed(
                title="❌ API Key Not Configured",
                description=(
                    "This bot doesn't have an ArchiveBox API key set.\n\n"
                    "Ask the bot owner to run:\n"
                    f"```\n{ctx.clean_prefix}set api archivebox api_key,<your_api_key>\n```"
                ),
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)

        url = self._normalize_url(url)

        if not self._is_valid_url(url):
            return await ctx.send("❌ Invalid URL. Must start with `http://` or `https://`.")

        pending_embed = discord.Embed(
            title="📦 Archiving...",
            description=f"Checking ArchiveBox for `{url}`...",
            color=discord.Color.orange()
        )
        pending_msg = await ctx.send(embed=pending_embed)

        async with aiohttp.ClientSession() as session:
            existing = await self._find_snapshot(session, api_key, base_url, url)
            if existing:
                embed = self._build_success_embed(existing, base_url, url, already_archived=True)
                await pending_msg.edit(embed=embed)
                return

            await pending_msg.edit(embed=discord.Embed(
                title="📦 Archiving...",
                description=f"Submitting `{url}` to ArchiveBox...",
                color=discord.Color.orange()
            ))

            endpoint = f"{base_url.rstrip('/')}/api/v1/cli/add"
            payload = {"urls": [url], "depth": 0, "max_urls": 0}

            try:
                async with session.post(
                    endpoint,
                    headers=self._get_headers(api_key),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    text = await resp.text()

                    if resp.status in (401, 403):
                        await pending_msg.edit(embed=discord.Embed(
                            title="❌ Authentication Failed",
                            description="The configured API key is invalid or has been revoked.",
                            color=discord.Color.red()
                        ))
                        return

                    if resp.status == 429:
                        await pending_msg.edit(embed=discord.Embed(
                            title="❌ Rate Limited",
                            description="ArchiveBox is rate-limiting requests. Please try again later.",
                            color=discord.Color.red()
                        ))
                        return

                    if resp.status >= 500:
                        await pending_msg.edit(embed=discord.Embed(
                            title="❌ ArchiveBox Server Error",
                            description=f"The ArchiveBox server returned HTTP {resp.status}. Please try again later.",
                            color=discord.Color.red()
                        ))
                        return

                    if resp.status != 200:
                        await pending_msg.edit(embed=discord.Embed(
                            title=f"❌ ArchiveBox Error (HTTP {resp.status})",
                            description=f"```\n{text[:1000]}\n```",
                            color=discord.Color.red()
                        ))
                        return

                    try:
                        data = await resp.json()
                    except Exception:
                        await pending_msg.edit(embed=discord.Embed(
                            title="❌ Invalid Response",
                            description="ArchiveBox returned an unexpected response format.",
                            color=discord.Color.red()
                        ))
                        return

            except aiohttp.ClientConnectorError:
                await pending_msg.edit(embed=discord.Embed(
                    title="❌ Connection Failed",
                    description=f"Could not connect to ArchiveBox at `{base_url}`.",
                    color=discord.Color.red()
                ))
                return
            except aiohttp.ServerTimeoutError:
                await pending_msg.edit(embed=discord.Embed(
                    title="❌ Request Timed Out",
                    description="ArchiveBox did not respond in time. It may be busy archiving.",
                    color=discord.Color.red()
                ))
                return
            except Exception as e:
                await pending_msg.edit(embed=discord.Embed(
                    title="❌ Unexpected Error",
                    description=f"```\n{type(e).__name__}: {e}\n```",
                    color=discord.Color.red()
                ))
                return

            snapshot = None
            for snap in self._extract_items(data):
                if isinstance(snap, dict):
                    snapshot = snap
                    break

            if snapshot and snapshot.get("timestamp") and self._status_ok(snapshot.get("status")):
                embed = self._build_success_embed(snapshot, base_url, url)
                await pending_msg.edit(embed=embed)
                return

            snapshot = await self._poll_for_snapshot(
                session, api_key, base_url, url, pending_msg
            )

            if snapshot:
                embed = self._build_success_embed(snapshot, base_url, url)
                await pending_msg.edit(embed=embed)
                return

            embed = discord.Embed(
                title="📦 Queued for Archiving",
                description=(
                    f"`{url}` has been submitted to ArchiveBox.\n\n"
                    f"The snapshot is still being generated in the background. "
                    f"You can check the archive later at:\n"
                    f"[ArchiveBox]({base_url})"
                ),
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Original URL", value=url[:1024], inline=False)
            await pending_msg.edit(embed=embed)

    @commands.group(name="archiveset")
    @commands.admin_or_permissions(manage_guild=True)
    async def archiveset(self, ctx: commands.Context):
        """Configure ArchiveBox settings."""
        pass

    @archiveset.command(name="url")
    async def archiveset_url(self, ctx: commands.Context, url: str):
        """Set the ArchiveBox server base URL."""
        url = self._normalize_url(url)
        if not self._is_valid_url(url):
            return await ctx.send("❌ Invalid URL. Must start with `http://` or `https://`.")
        await self.config.base_url.set(url.rstrip("/"))
        await ctx.send(f"✅ ArchiveBox base URL set to `{url}`.")

    @archiveset.command(name="show")
    async def archiveset_show(self, ctx: commands.Context):
        """Show current ArchiveBox configuration."""
        api_key = await self._get_api_key()
        base_url = await self.config.base_url()

        key_display = "✅ Configured" if api_key else "❌ Not set"

        embed = discord.Embed(
            title="⚙️ ArchiveBox Configuration",
            color=await ctx.embed_color()
        )
        embed.add_field(name="Base URL", value=base_url, inline=False)
        embed.add_field(name="API Key", value=key_display, inline=False)
        embed.add_field(
            name="How to set API key",
            value=f"Run `{ctx.clean_prefix}set api archivebox api_key,<your_api_key>` (bot owner only)",
            inline=False
        )
        await ctx.send(embed=embed)

    @commands.command(name="archivestatus")
    @commands.mod_or_permissions(manage_messages=True)
    async def archivestatus(self, ctx: commands.Context):
        """Check if ArchiveBox is reachable and the API key is valid."""
        api_key = await self._get_api_key()
        base_url = await self.config.base_url()

        if not api_key:
            return await ctx.send(
                f"❌ No API key configured. Use `{ctx.clean_prefix}set api archivebox api_key,<key>` to set one."
            )

        endpoint = f"{base_url.rstrip('/')}/api/v1/core/snapshots"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    headers=self._get_headers(api_key),
                    params={"limit": 1, "offset": 0},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        embed = discord.Embed(
                            title="✅ ArchiveBox is Online",
                            description=f"Successfully connected to `{base_url}`.",
                            color=discord.Color.green()
                        )
                    elif resp.status in (401, 403):
                        embed = discord.Embed(
                            title="❌ Invalid API Key",
                            description="The server rejected the API key.",
                            color=discord.Color.red()
                        )
                    else:
                        embed = discord.Embed(
                            title=f"⚠️ ArchiveBox returned HTTP {resp.status}",
                            description="The server responded but with an unexpected status.",
                            color=discord.Color.orange()
                        )
        except aiohttp.ClientConnectorError:
            embed = discord.Embed(
                title="❌ Connection Failed",
                description=f"Could not reach ArchiveBox at `{base_url}`.",
                color=discord.Color.red()
            )
        except Exception as e:
            embed = discord.Embed(
                title="❌ Error",
                description=f"```\n{type(e).__name__}: {e}\n```",
                color=discord.Color.red()
            )

        await ctx.send(embed=embed)

    # ── Slash commands ──

    @app_commands.command(name="archive", description="Submit a URL to ArchiveBox for archiving")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(url="The URL to archive")
    async def slash_archive(self, interaction: discord.Interaction, url: str):
        """Slash command: submit a URL to ArchiveBox."""
        api_key = await self._get_api_key()
        base_url = await self.config.base_url()

        if not api_key:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ API Key Not Configured",
                    description=(
                        "This bot doesn't have an ArchiveBox API key set.\n\n"
                        "Ask the bot owner to run:\n"
                        "```\n/set api archivebox api_key,<your_api_key>\n```"
                    ),
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

        url = self._normalize_url(url)

        if not self._is_valid_url(url):
            return await interaction.response.send_message(
                "❌ Invalid URL. Must start with `http://` or `https://`.",
                ephemeral=True
            )

        await interaction.response.defer(thinking=True)

        async with aiohttp.ClientSession() as session:
            existing = await self._find_snapshot(session, api_key, base_url, url)
            if existing:
                embed = self._build_success_embed(existing, base_url, url, already_archived=True)
                return await interaction.edit_original_response(embed=embed)

            endpoint = f"{base_url.rstrip('/')}/api/v1/cli/add"
            payload = {"urls": [url], "depth": 0, "max_urls": 0}

            try:
                async with session.post(
                    endpoint,
                    headers=self._get_headers(api_key),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    text = await resp.text()

                    if resp.status in (401, 403):
                        return await interaction.edit_original_response(embed=discord.Embed(
                            title="❌ Authentication Failed",
                            description="The configured API key is invalid or has been revoked.",
                            color=discord.Color.red()
                        ))

                    if resp.status == 429:
                        return await interaction.edit_original_response(embed=discord.Embed(
                            title="❌ Rate Limited",
                            description="ArchiveBox is rate-limiting requests. Please try again later.",
                            color=discord.Color.red()
                        ))

                    if resp.status >= 500:
                        return await interaction.edit_original_response(embed=discord.Embed(
                            title="❌ ArchiveBox Server Error",
                            description=f"The ArchiveBox server returned HTTP {resp.status}. Please try again later.",
                            color=discord.Color.red()
                        ))

                    if resp.status != 200:
                        return await interaction.edit_original_response(embed=discord.Embed(
                            title=f"❌ ArchiveBox Error (HTTP {resp.status})",
                            description=f"```\n{text[:1000]}\n```",
                            color=discord.Color.red()
                        ))

                    try:
                        data = await resp.json()
                    except Exception:
                        return await interaction.edit_original_response(embed=discord.Embed(
                            title="❌ Invalid Response",
                            description="ArchiveBox returned an unexpected response format.",
                            color=discord.Color.red()
                        ))

            except aiohttp.ClientConnectorError:
                return await interaction.edit_original_response(embed=discord.Embed(
                    title="❌ Connection Failed",
                    description=f"Could not connect to ArchiveBox at `{base_url}`.",
                    color=discord.Color.red()
                ))
            except aiohttp.ServerTimeoutError:
                return await interaction.edit_original_response(embed=discord.Embed(
                    title="❌ Request Timed Out",
                    description="ArchiveBox did not respond in time. It may be busy archiving.",
                    color=discord.Color.red()
                ))
            except Exception as e:
                return await interaction.edit_original_response(embed=discord.Embed(
                    title="❌ Unexpected Error",
                    description=f"```\n{type(e).__name__}: {e}\n```",
                    color=discord.Color.red()
                ))

            snapshot = None
            for snap in self._extract_items(data):
                if isinstance(snap, dict):
                    snapshot = snap
                    break

            if snapshot and snapshot.get("timestamp") and self._status_ok(snapshot.get("status")):
                embed = self._build_success_embed(snapshot, base_url, url)
                return await interaction.edit_original_response(embed=embed)

            snapshot = await self._slash_poll_for_snapshot(
                session, api_key, base_url, url, interaction
            )

            if snapshot:
                embed = self._build_success_embed(snapshot, base_url, url)
                return await interaction.edit_original_response(embed=embed)

            embed = discord.Embed(
                title="📦 Queued for Archiving",
                description=(
                    f"`{url}` has been submitted to ArchiveBox.\n\n"
                    f"The snapshot is still being generated in the background. "
                    f"You can check the archive later at:\n"
                    f"[ArchiveBox]({base_url})"
                ),
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Original URL", value=url[:1024], inline=False)
            await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="archivestatus", description="Check if ArchiveBox is online and the API key is valid")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def slash_archivestatus(self, interaction: discord.Interaction):
        """Slash command: check ArchiveBox status."""
        api_key = await self._get_api_key()
        base_url = await self.config.base_url()

        if not api_key:
            return await interaction.response.send_message(
                "❌ No API key configured. Use `/set api archivebox api_key,<key>` to set one.",
                ephemeral=True
            )

        await interaction.response.defer(thinking=True)

        endpoint = f"{base_url.rstrip('/')}/api/v1/core/snapshots"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    headers=self._get_headers(api_key),
                    params={"limit": 1, "offset": 0},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        embed = discord.Embed(
                            title="✅ ArchiveBox is Online",
                            description=f"Successfully connected to `{base_url}`.",
                            color=discord.Color.green()
                        )
                    elif resp.status in (401, 403):
                        embed = discord.Embed(
                            title="❌ Invalid API Key",
                            description="The server rejected the API key.",
                            color=discord.Color.red()
                        )
                    else:
                        embed = discord.Embed(
                            title=f"⚠️ ArchiveBox returned HTTP {resp.status}",
                            description="The server responded but with an unexpected status.",
                            color=discord.Color.orange()
                        )
        except aiohttp.ClientConnectorError:
            embed = discord.Embed(
                title="❌ Connection Failed",
                description=f"Could not reach ArchiveBox at `{base_url}`.",
                color=discord.Color.red()
            )
        except Exception as e:
            embed = discord.Embed(
                title="❌ Error",
                description=f"```\n{type(e).__name__}: {e}\n```",
                color=discord.Color.red()
            )

        await interaction.edit_original_response(embed=embed)
