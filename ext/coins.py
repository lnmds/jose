import logging
import asyncio
import decimal
import sys
import pprint
import time
import random
import math

import discord
from discord.ext import commands

from .common import Cog, CoinConverter
from .utils import Timer

sys.path.append('..')
from jcoin.errors import GenericError, TransferError, \
        AccountNotFoundError, InputError, ConditionError, err_list

log = logging.getLogger(__name__)
REWARD_COOLDOWN = 18000

TAX_MULTIPLIER = decimal.Decimal('1.42')


class AccountType:
    """Account types."""
    USER = 0
    TAXBANK = 1


class Coins(Cog):
    """JoséCoin v3
    """

    def __init__(self, bot):
        super().__init__(bot)
        self.base_url = bot.config.JOSECOIN_API
        self.bot.simple_exc.extend(err_list)
        self.transfers_done = 0

        #: Reward cooldowns are stored here
        self.rewards = {}

        #: Cache for probability values
        self.prob_cache = {}

        self.AccountType = AccountType
        self.AccountNotFoundError = AccountNotFoundError
        self.TransferError = TransferError
        self.ConditionError = ConditionError

    def _route(self, route):
        return f'{self.base_url}{route}'

    @property
    def headers(self):
        """Get the headers for a josécoin request."""
        return {'Authorization': self.bot.config.JOSECOIN_TOKEN}

    async def generic_call(self,
                           method: str,
                           route: str,
                           payload: dict = None,
                           **kwargs) -> 'any':
        """Generic call to any JoséCoin API route."""
        route = self._route(route)
        headers = self.headers
        async with self.bot.session.request(
                method, route, json=payload, headers=headers) as resp:

            if kwargs.get('log', True):
                log.debug(f'called {route!r}, status {resp.status}')

            if resp.status == 500:
                raise Exception('Internal Server Error')

            data = await resp.json()
            if isinstance(data, dict) and data.get('error'):
                msg = data.get('message')
                for exc in err_list:
                    if exc.status_code == resp.status:
                        if exc == AccountNotFoundError:
                            raise AccountNotFoundError("Account not found, rea"
                                                       "d the documentation "
                                                       "at 'j!help Coins' "
                                                       "(CASE-SENSITIVE)")
                        raise exc(msg)

                # generic exception for unknown error codes
                raise Exception(msg)

            return data

    def jc_get(self, route: str, payload: dict = None, **kwargs):
        """Make a GET request to JoséCoin API."""
        return self.generic_call('GET', route, payload, **kwargs)

    def jc_post(self, route: str, payload: dict = None, **kwargs):
        """Calls a route with POST."""
        return self.generic_call('POST', route, payload, **kwargs)

    def jc_delete(self, route: str, payload: dict = None, **kwargs):
        """Calls a route with DELETE."""
        return self.generic_call('DELETE', route, payload, **kwargs)

    def get_name_raw(self, user_id: int, account=None):
        """Get a string representation of a user or guild.

        Parameters
        ----------
        user_id: int
            User ID to get a string representation of.
        account: dict, optional
            Account object to determine if this is
            A user or a guild to search.

        Returns
        -------
        str
        """
        if isinstance(user_id, discord.Guild):
            return str(f'taxbank:{user_id.name}')
        elif isinstance(user_id, (discord.User, discord.Member)):
            return str(user_id)

        obj = self.bot.get_user(int(user_id))

        if not obj:
            # try to find guild
            obj = self.bot.get_guild(user_id)
            if obj:
                obj = f'taxbank:{obj}'

        if not obj:
            # we tried stuff, show a special text
            if account:
                res = ''
                if account['account_type'] == AccountType.USER:
                    res = f'Unfindable User {user_id}'
                elif account['account_type'] == AccountType.TAXBANK:
                    res = f'Unfindable Guild {user_id}'
                else:
                    res = f'Unfindable Unknown {user_id}'
                return res
            else:
                return f'Unfindable ID {user_id}'

        return str(obj)

    def get_name(self, *args, **kwargs):
        """Clean the content of get_name call."""
        res = self.get_name_raw(*args, **kwargs)
        return self.bot.clean_content(res)

    async def get_account(self, wallet_id: int) -> dict:
        """Get an account"""
        if getattr(wallet_id, 'id', None):
            wallet_id = wallet_id.id

        r = await self.jc_get(f'/wallets/{wallet_id}', log=False)
        r['amount'] = decimal.Decimal(f'{r["amount"]:.3f}')
        try:
            r['taxpaid'] = decimal.Decimal(f'{r["taxpaid"]:.3f}')
        except KeyError:
            pass

        try:
            r['ubank'] = decimal.Decimal(f'{r["ubank"]:.3f}')
        except KeyError:
            pass

        return r

    async def create_wallet(self, thing):
        """Send a request to create a JoséCoin account."""

        acc_type = AccountType.USER \
            if isinstance(thing, discord.abc.User) else \
            AccountType.TAXBANK

        rows = await self.jc_post(f'/wallets/{thing.id}', {
            'type': acc_type,
        })

        if rows:
            log.info('Created account for %r[%d]', thing, thing.id)

        return rows

    async def ensure_ctx(self, ctx):
        """Ensure that things are sane, given ctx."""
        try:
            await self.create_wallet(ctx.bot.user)
        except self.ConditionError:
            pass

        try:
            await self.ensure_taxbank(ctx)
        except self.ConditionError:
            pass

    async def get_ranks(self, account_id: int, guild_id=None) -> dict:
        """Get rank information.

        Parameters
        ----------
        account_id: int
            The user's account ID to compare with the guild accounts
        guild_id: int
            The guild's ID.
        """
        rank_data = await self.jc_get(f'/wallets/{account_id}/rank',
                                      {'guild_id': guild_id})
        return rank_data

    async def ranks(self, user_id: int, guild: discord.Guild) -> tuple:
        """Get rank info about a user, gives both global and local.

        This method is compatible with JoséCoin v2.
        """
        rank_data = await self.get_ranks(user_id, guild.id)
        rdl = rank_data['local']
        rdg = rank_data['global']
        return rdl['rank'], rdg['rank'], rdl['total'], rdg['total']

    async def transfer(self, from_id: int, to_id: int,
                       amount: decimal.Decimal) -> dict:
        """Make the transfer call"""

        _from_id = getattr(from_id, 'id', False)
        if _from_id:
            from_id = _from_id

        _to_id = getattr(to_id, 'id', False)
        if _to_id:
            to_id = _to_id

        res = await self.jc_post(
            f'/wallets/{from_id}/transfer', {
                'receiver': to_id,
                'amount': str(amount)
            },
            log=False)

        sender_name = self.get_name(from_id)
        receiver_name = self.get_name(to_id)

        self.transfers_done += 1
        msg = f'{sender_name} > {amount} > {receiver_name}'
        log.info(msg)

        # NOTE: log level 60 is used by the ChannelLogging cog!
        log.log(60, msg)

        return res

    async def transfer_str(self, from_id: int, to_id: int,
                           amount: decimal.Decimal) -> str:
        """Transfer between accounts, but returning a string."""
        await self.transfer(from_id, to_id, amount)
        return f'{self.get_name(from_id)} > {amount} > {self.get_name(to_id)}'

    async def ensure_taxbank(self, ctx):
        """Ensure a taxbank exists for the guild."""
        if ctx.guild is None:
            raise self.SayException('You cannot do this in a DM.')

        try:
            await self.get_account(ctx.guild.id)
            return
        except AccountNotFoundError:
            await self.create_wallet(ctx.guild)

    async def get_gdp(self) -> decimal.Decimal:
        """Get the economy's gdp."""
        gdp = await self.jc_get('/gdp')
        return decimal.Decimal(gdp['gdp'])

    async def sink(self, user_id: int, amount: decimal.Decimal):
        """Send money back to José."""
        return await self.transfer(user_id, self.bot.user.id, amount)

    async def zero(self, user_id: int, where=None) -> str:
        """Zero an account.

        This transfers all their money to José.
        """
        account = await self.get_account(user_id)
        target = where or self.bot.user.id

        # nope, can not use sink()
        return await self.transfer_str(user_id, target, account['amount'])

    def to_acclist(self, users: list) -> list:
        """Convert a list of user objects to a list of account IDs."""
        account_ids = []
        for u in users:
            uid = getattr(u, 'id', None)
            if uid:
                account_ids.append(uid)
                continue

            account_ids.append(u)

        return account_ids

    async def lock(self, *users):
        """Lock accounts from transfer operations."""
        await self.jc_post('/lock_accounts',
                           {'accounts': self.to_acclist(users)})

    async def unlock(self, *users):
        """Unlock accounts from transfer operations."""
        await self.jc_post('/unlock_accounts',
                           {'accounts': self.to_acclist(users)})

    async def is_locked(self, account_id: int):
        """Check if an account is locked"""
        return await self.jc_get('/check_lock', {'account_id': account_id})

    def _pcache_invalidate(self, user_id: int):
        """Invalidate the prob cache after 2 hours for one user."""
        # log.debug(f'popping {user_id} from cache')
        try:
            self.prob_cache.pop(user_id)
        except KeyError:
            pass

    def pcache_set(self, author_id: int, value):
        """Set a value for a user in the probability cache"""
        self.prob_cache[author_id] = value

        # NOTE: this feels like Redis, but I don't want to use
        #  Redis, so we use a dict! (webscale! no sockets!)
        self.loop.call_later(7200, self._pcache_invalidate, author_id)

    async def pricing(self, ctx, base_tax: decimal.Decimal) -> str:
        """Tax someone."""
        await self.ensure_ctx(ctx)
        base_tax = decimal.Decimal(base_tax)

        try:
            account = await self.get_account(ctx.author.id)
        except AccountNotFoundError:
            raise self.SayException("You don't have a JoséCoin wallet, "
                                    f'use the `account` command.')

        # use both amount and ubank to calc taxpay
        amount = account['amount'] + account['ubank']

        gdp = await self.jc_get('/gdp')
        gdp = gdp['gdp']

        gdp_sqrt = decimal.Decimal(math.sqrt(gdp))
        total_tax = base_tax + pow((amount / gdp_sqrt) * TAX_MULTIPLIER, 2)
        try:
            await self.transfer(ctx.author.id, ctx.guild.id, total_tax)
        except self.ConditionError as err:
            raise self.SayException(f'TransferError: `{err.args[0]}`')

    async def on_message(self, message):
        """Manage autocoin."""
        # ignore bots and DMs
        if message.author.bot or not message.guild:
            return

        author_id = message.author.id
        guild_id = message.guild.id

        user_blocked = await self.bot.is_blocked(author_id)
        guild_blocked = await self.bot.is_blocked_guild(guild_id)
        if user_blocked or guild_blocked:
            return

        now = time.monotonic()

        cext = self.bot.get_cog('CoinsExt')
        if not cext:
            return

        # This is a better idea than copypasting the
        # code for check_cooldowns here.
        try:
            await cext.check_cooldowns(message.author)
        except self.SayException as err:
            msg = err.args[0]
            if 'prison' in msg:
                return

        # manage reward cooldowns
        last_reward = self.rewards.get(author_id, 0)
        if now < last_reward:
            return

        # check the user's probability
        try:
            if author_id in self.prob_cache:
                probdata = self.prob_cache[author_id]
            else:
                probdata = await self.jc_get(
                    f'/wallets/{author_id}/'
                    'probability', log=False)
                self.pcache_set(author_id, probdata)
        except AccountNotFoundError:
            self.pcache_set(author_id, None)
            return

        if not probdata:
            return

        prob = probdata['probability']
        prob = float(prob)
        if random.random() > prob:
            return

        to_give = round(random.uniform(0, 1.1), 2)
        if to_give < 0.3:
            return

        try:
            await self.transfer(self.bot.user.id, author_id, to_give)

            self.rewards[author_id] = time.monotonic() + REWARD_COOLDOWN
            if message.guild.large:
                return

            hc = await self.jc_get(f'/wallets/{author_id}/hidecoin_status')
            # hc = {'hidden': True}
            if hc['hidden']:
                return

            try:
                await message.add_reaction('\N{MONEY BAG}')
            except Exception as e:
                log.exception('autocoin failed to add reaction')
        except Exception as e:
            log.exception('autocoin error')

    @commands.command()
    async def account(self, ctx):
        """Create a JoséCoin wallet.

        How do get money????

        JoséCoins work based on your activity.
        This mean that with every message that you send
        you have a probability of getting a random JC reward.

        Upon getting the reward, José will react to your message
        with 💰. If you want to not show it, use j!hidecoins.

        No, spamming won't work. Rewards have a 1 hour cooldown.
        """
        try:
            await self.ensure_taxbank(ctx)
            await self.get_account(ctx.author.id)
            return await ctx.send('You already have an account.')
        except AccountNotFoundError:
            await self.create_wallet(ctx.author)
            await ctx.send('Please read the docs at `j!help account`')
            await ctx.ok()
        except Exception as err:
            await ctx.not_ok()

            # raise it to logging
            raise err

    @commands.command(aliases=['balance', 'bal'])
    async def wallet(self, ctx, person: discord.User = None):
        """Check your wallet details."""
        if not person:
            person = ctx.author

        account = await self.get_account(person.id)

        await ctx.send(f'`{self.get_name(person.id)}` : '
                       f'`{account["amount"]:.2f}JC`, paid '
                       f'`{account["taxpaid"]:.2f}JC` as tax.\n'
                       f'`{account["ubank"]:.2f}JC` in the personal bank.')

    @commands.command()
    async def bankdeposit(self, ctx, amount: CoinConverter):
        """Deposit in your personal bank.

        You can not withdraw from this bank.

        Nobody else (other than José) can access
        this money once deposited.
        """
        resp = await self.jc_post(f'/wallets/{ctx.author.id}/deposit', {
            'amount': str(amount),
        })

        await ctx.success(resp['status'])

    @commands.command(aliases=['txw', 'txb', 'txbal', 'txbalance'])
    async def txwallet(self, ctx, guild_id: int = None):
        """Check a taxbank's wallet.

        Shows the current taxbank as default.
        """
        # NOTE: we don't use discord.Guild converter
        #  because in the case jose leaves a guild we should
        #  kinda still be able to query a taxbank

        # TODO: maybe a custom converter..? TaxbankAccount...?

        if not guild_id:
            guild_id = ctx.guild.id

        acc = await self.get_account(guild_id)

        await ctx.send(f'\N{BANK} `{self.get_name(acc["account_id"])}` > '
                       f'`{acc["amount"]:.2f}JC`')

    @commands.command(name='transfer')
    async def _transfer(self, ctx, receiver: discord.User,
                        amount: CoinConverter):
        """Transfer coins between you and someone else."""
        if receiver.bot:
            raise self.SayException('Receiver can not be a bot')

        await self.transfer(ctx.author.id, receiver.id, amount)
        await ctx.send(f'\N{MONEY WITH WINGS} `{ctx.author!s}` > '
                       f'`{amount}JC` > `{receiver!s}` \N{MONEY BAG}')

    @commands.command()
    async def donate(self, ctx, amount: CoinConverter):
        """Donate to the guild's taxbank."""
        await self.transfer(ctx.author.id, ctx.guild.id, amount)
        await ctx.send(f'\N{MONEY WITH WINGS} `{ctx.author!s}` > '
                       f'`{amount}JC` > `{ctx.guild!s}` \N{MONEY BAG}')

    @commands.command(name='ranks')
    @commands.guild_only()
    async def _ranks(self, ctx, person: discord.User = None):
        """Get rank data from someone."""
        if not person:
            person = ctx.author

        res = await self.get_ranks(person.id, ctx.guild.id)
        em = discord.Embed(
            title=f'Rank data for {person}', color=discord.Color(0x540786))

        for cat in res:
            data = res[cat]
            em.add_field(
                name=cat.capitalize(),
                value=f'#{data["rank"]} out from '
                f'{data["total"]} accounts',
                inline=False)

        await ctx.send(embed=em)

    @commands.command(aliases=['jcp'])
    async def jcping(self, ctx):
        """Check if the JoséCoin API is up."""
        res = None
        with Timer() as timer:
            try:
                res = await self.jc_get('/health')
                alive = res['status']

                if not alive:
                    return await ctx.send('JoséCoin API is not ok.')
            except Exception as e:
                return await ctx.send(f'Failed to contact JoséCoin API {e!r}')

        await ctx.send(f'`{timer}`, db: `{res["db_latency_sec"]*1000}ms`')

    @commands.command()
    async def coinprob(self, ctx, person: discord.User = None):
        """Get your coin probability values.

        Use 'j!help account' to know what does it mean.
        """
        if not person:
            person = ctx.author

        data = await self.jc_get(f'/wallets/{person.id}/probability')
        prob = float(data['probability'])
        await ctx.send(f'You have a {prob * 100}%/message chance')

    @commands.command()
    async def jcgetraw(self, ctx):
        """Get the raw information on your wallet."""
        with Timer() as timer:
            wallet = await self.get_account(ctx.author.id)
        res = pprint.pformat(wallet)
        await ctx.send(f'```python\n{res}\nTook {timer}\n```')

    @commands.command()
    @commands.is_owner()
    async def write(self, ctx, person: discord.User, amount: str):
        """Overwrite someone's wallet.

        Only bot owner can use this command.
        """
        with Timer() as timer:
            await self.pool.execute("""
            UPDATE accounts
            SET amount=$1
            WHERE account_id=$2
            """, amount, person.id)

        await ctx.send(f'write took {timer}')

    @commands.command()
    @commands.is_owner()
    async def spam(self, ctx, taskcount: int = 200, timeout: int = 30):
        """webscale memes.

        This was made to spam the current API and know its limits.
        Only bot owner can use this command.

        This will spawn an initial amount of [taskcount] tasks,
        with each one doing a transfer call.

        If all tasks complete the operation, we double the amount of tasks.

        If we hit a timeout, this stops.
        """
        while True:
            tasks = []
            done, pending = None, None

            with Timer() as timer:
                for i in range(taskcount):
                    coro = self.transfer(ctx.bot.user.id, ctx.author.id, 0.1)
                    t = self.loop.create_task(coro)
                    tasks.append(t)

                done, pending = await asyncio.wait(tasks, timeout=timeout)

            self.loop.create_task(ctx.send(f'{done} done tasks in '
                                           f'{timer} ({pending} pending)'))

            if pending:
                return await ctx.send(f'pending {len(pending)} > 0')

            taskcount *= 2

    @commands.command()
    async def deleteaccount(self, ctx, confirm: bool = False):
        """Delete your JoséCoin account.

        There is no going back from this operation.

        Use "y", "yes", and variants, to confirm it.
        """
        if not confirm:
            return await ctx.send('You did not confirm the operation.')

        log.warning(f'deleting account {ctx.author!r}')

        res = await self.jc_delete(f'/wallets/{ctx.author.id}')
        await ctx.status(res['success'])

    @commands.command()
    async def hidecoins(self, ctx):
        """Toggle the coin reaction in your account"""
        result = await self.jc_post(f'/wallets/{ctx.author.id}/hidecoins')
        result = result['new_hidecoins']
        resultstr = 'on' if result else 'off'
        await ctx.send(f'no reactions are set to `{resultstr}` for you.')

    @commands.group(invoke_without_command=True)
    async def jcstats(self, ctx):
        """Get josécoin stats"""
        em = discord.Embed(title='josécoin stats', color=discord.Color.gold())

        stats = await self.jc_get('/stats')

        em.add_field(
            name='total transfers currently', value=self.transfers_done)

        em.add_field(name='total accounts', value=stats['accounts'])
        em.add_field(name='total user accounts', value=stats['user_accounts'])
        em.add_field(name='total taxbanks', value=stats['txb_accounts'])

        em.add_field(name='total money', value=stats['gdp'])
        em.add_field(name='total user money', value=stats['user_money'])
        em.add_field(name='total taxbank money', value=stats['txb_money'])

        em.add_field(name='total steals done', value=stats['steals'])
        em.add_field(name='total steal success', value=stats['success'])
        await ctx.send(embed=em)

    def steal_fmt(self, row) -> str:
        if not row:
            return None

        thief_name = self.jcoin.get_name(row['thief'])
        target_name = self.jcoin.get_name(row['target'])
        return (f'#{row["idx"]} - `{thief_name}` stealing '
                f'`{row["amount"]}` from `{target_name}` | '
                f'chance: {row["chance"]}, res: {row["res"]}')

    @jcstats.command(name='steals', aliases=['s'])
    async def steal_stats(self, ctx):
        """Get josécoin steal stats."""
        em = discord.Embed(title='Steal stats', color=discord.Color(0xdabdab))

        tot_stolen = await self.pool.fetchval("""
            select sum(amount) from steal_history
            where success=true
        """)

        max_amount = await self.pool.fetchrow("""
            select * from steal_history
            where amount = (
                select max(amount) from steal_history
                where success = true
                )
                and success = true
            limit 1
        """)

        min_res = await self.pool.fetchrow("""
            select * from steal_history
            where res = (
                select min(res) from steal_history
                where success = true
                )
                and success = true
            limit 1
        """)

        min_chance = await self.pool.fetchrow("""
            select * from steal_history
            where chance = (
                select min(chance) from steal_history
                where success = true
                )
                and success = true
            limit 1
        """)

        em.add_field(name='total jc stolen', value=tot_stolen, inline=False)

        em.add_field(
            name='maximum amount successfully stolen',
            value=self.steal_fmt(max_amount) or '<none>',
            inline=False)

        em.add_field(
            name='success steal with min res (most lucky)',
            value=self.steal_fmt(min_res) or '<none>',
            inline=False)

        em.add_field(
            name='success steal with min chance (most difficult)',
            value=self.steal_fmt(min_chance) or '<none>',
            inline=False)

        await ctx.send(embed=em)


def setup(bot):
    bot.add_cog(Coins(bot))
