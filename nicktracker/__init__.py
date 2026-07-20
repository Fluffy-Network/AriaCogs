from .nicktracker import NicknameTracker


async def setup(bot):
    await bot.add_cog(NicknameTracker(bot))
