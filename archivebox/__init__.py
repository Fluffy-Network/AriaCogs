from .archivebox import ArchiveBox


async def setup(bot):
    await bot.add_cog(ArchiveBox(bot))
