import asyncio
import aiohttp
import discord
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

    def _extract_results(self, data) -> list:
        """Extract a list of snapshot dicts from various ArchiveBox response shapes."""
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
            # The dict itself might be a single snapshot
            return [data]
        return []

    async def _find_snapshot(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str,
        target_url: str
    ) -> dict | None:
        """Search ArchiveBox for an existing snapshot of target_url."""
        endpoint = f"{base_url.rstrip('/')}/api/v1/core/snapshot"
        headers = self._get_headers(api_key)

        # Try filtering by URL first (most ArchiveBox versions support this)
        for param_name in ("url", "filter", "q"):
            try:
                async with session.get(
                    endpoint,
                    headers=headers,
                    params={param_name: target_url, "limit": 5},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for snapshot in self._extract_results(data):
                            if isinstance(snapshot, dict):
                                snap_url = snapshot.get("url", "")
                                if self._urls_match(snap_url, target_url) and snapshot.get("timestamp"):
                                    return snapshot
            except Exception:
                pass

        # Fallback: fetch the most recent snapshots and scan client-side
        try:
            async with session.get(
                endpoint,
                headers=headers,
                params={"limit": 50, "offset": 0},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for snapshot in self._extract_results(data):
                        if isinstance(snapshot, dict):
                            snap_url = snapshot.get("url", "")
                            if self._urls_match(snap_url, target_url) and snapshot.get("timestamp"):
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

            # Update the pending message so the user knows we're still waiting
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

    @commands.command(name="archive")
    @commands.mod_or_permissions(manage_messages=True)
    async def archive(self, ctx: commands.Context, url: str):
        """Submit a URL to ArchiveBox for archiving.

        If the URL has already been archived, the existing snapshot is returned
        instead of creating a duplicate.

        **Usage:**
        `[p]archive https://example.com`
        `[p]archive <https://example.com>`

        The bot owner must configure an API key first with `[p]set api archivebox api_key,<key>`.
        """
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

        # Send a temporary "archiving..." message
        pending_embed = discord.Embed(
            title="📦 Archiving...",
            description=f"Checking ArchiveBox for `{url}`...",
            color=discord.Color.orange()
        )
        pending_msg = await ctx.send(embed=pending_embed)

        async with aiohttp.ClientSession() as session:
            # ── 1. Check if the URL is already archived ──
            existing = await self._find_snapshot(session, api_key, base_url, url)
            if existing and existing.get("timestamp"):
                timestamp = existing.get("timestamp")
                title = existing.get("title")
                archived_url = existing.get("url", url)
                status = existing.get("status", "unknown")
                tags = existing.get("tags", "")
                archive_link = f"{base_url.rstrip('/')}/archive/{timestamp}"

                color = discord.Color.green() if status == "succeeded" else discord.Color.blurple()

                embed = discord.Embed(
                    title="📦 Already Archived",
                    description="This URL was already in ArchiveBox — no duplicate was created.",
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
                await pending_msg.edit(embed=embed)
                return

            # ── 2. Submit the URL for archiving ──
            await pending_msg.edit(embed=discord.Embed(
                title="📦 Archiving...",
                description=f"Submitting `{url}` to ArchiveBox...",
                color=discord.Color.orange()
            ))

            endpoint = f"{base_url.rstrip('/')}/api/v1/cli/add"
            payload = {
                "urls": [url],
                "depth": 0,
                "max_urls": 0
            }

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

            # ── 3. Parse the immediate response ──
            snapshot = None
            for snap in self._extract_results(data):
                if isinstance(snap, dict):
                    snapshot = snap
                    break

            if snapshot and snapshot.get("timestamp"):
                # Snapshot was ready immediately
                timestamp = snapshot.get("timestamp")
                title = snapshot.get("title")
                archived_url = snapshot.get("url", url)
                status = snapshot.get("status", "unknown")
                tags = snapshot.get("tags", "")
                archive_link = f"{base_url.rstrip('/')}/archive/{timestamp}"

                color = discord.Color.green() if status == "succeeded" else discord.Color.blurple()

                embed = discord.Embed(
                    title="📦 URL Archived",
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
                await pending_msg.edit(embed=embed)
                return

            # ── 4. Poll until the snapshot appears ──
            snapshot = await self._poll_for_snapshot(
                session, api_key, base_url, url, pending_msg
            )

            if snapshot and snapshot.get("timestamp"):
                timestamp = snapshot.get("timestamp")
                title = snapshot.get("title")
                archived_url = snapshot.get("url", url)
                status = snapshot.get("status", "unknown")
                tags = snapshot.get("tags", "")
                archive_link = f"{base_url.rstrip('/')}/archive/{timestamp}"

                color = discord.Color.green() if status == "succeeded" else discord.Color.blurple()

                embed = discord.Embed(
                    title="📦 URL Archived",
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
                await pending_msg.edit(embed=embed)
                return

            # ── 5. Timed out — still show the queued message ──
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

        endpoint = f"{base_url.rstrip('/')}/api/v1/core/snapshot"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    headers=self._get_headers(api_key),
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
