from .scheduledmsg import ScheduledMessage


async def setup(bot):
    await bot.add_cog(ScheduledMessage(bot))
