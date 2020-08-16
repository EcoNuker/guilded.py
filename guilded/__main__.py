import re
import abc
import json
import uuid
import shlex
import aiohttp
import asyncio
import datetime
import traceback
import websockets
BASE   = 'https://api.guilded.gg/'
WS_URL = 'wss://api.guilded.gg/socket.io/?jwt=undefined&EIO=3&transport=websocket'

session = None
async def make_session():
    global session
    session = aiohttp.ClientSession()

def make_datetime(initial):
    # for dates formatted like 2020-07-28T22:28:01.151Z
    #                          yyyy-mm-ssThh:mm:ss.mlsZ
    try:
        j1        = initial.split('T')
        j1_date   = j1[0].split('-')
        j1_time   = j1[1].split(':')
        j1_year   = int(j1_date[0])
        j1_month  = int(j1_date[1])
        j1_day    = int(j1_date[2])
        j1_hour   = int(j1_time[0])
        j1_minute = int(j1_time[1])
        j1_second = int(re.sub(r'\..+Z$', '', j1_time[2]))
        finalDate = datetime.datetime(year=j1_year, month=j1_month, day=j1_day, hour=j1_hour, minute=j1_minute, second=j1_second)
        return finalDate

    except: 
        # will make this more.. usable eventually
        return initial

class Bot:
    def __init__(self, command_prefix, **kwargs):
        # ""Settings""
        self.command_prefix = command_prefix
        self.loop           = kwargs.get('loop', asyncio.get_event_loop())
        self.description    = kwargs.get('description', None)
        self.owner_id       = kwargs.get('owner_id')
        # To be assigned upon start
        self.user           = None # ClientUser object
        self.login_cookie   = None
        ## Internal
        self.listeners      = []
        self.commands       = []
        # Cache
        self.teams          = []
        self.team_groups    = []
        self.text_channels  = []
        self.channels       = []
        self.users          = []

    # grab from cache
    def get_team(self, teamId):
        team = None
        for t in self.teams:
            if t.id == teamId:
                team = t
                break
        return team

    def get_team(self, userId):
        user = None
        for u in self.users:
            if u.id == userId:
                user = t
                break
        return user

    # fetch from the api
    async def fetch_team(self, teamId):
        teamResponse = await session.get(BASE + 'teams/' + teamId)
        teamJson     = (await teamResponse.json())['team']
        team         = Team(**teamJson)
        for t in self.teams:
            if t.id == team.id:
                self.teams.remove(t)
        self.teams.append(team)
        return team

    async def fetch_user(self, userId):
        userResponse = await session.get(BASE + 'users/' + userId)
        userJson     = (await userResponse.json())['user']
        user         = User(**userJson)
        for u in self.users:
            if u.id == user.id:
                self.users.remove(u)
        self.users.append(user)
        return user

    # on_ready event, obv
    async def trigger_on_ready(self):
        for f in self.listeners:
            if f.__name__ == 'on_ready':
                await f.__call__()

    # connection
    async def heartbeat(self, websocket):
        while True:
            await asyncio.sleep(25)
            try:
                await websocket.send('2')
            except:
                await self.connect(cookie=self.login_cookie)
                await self.trigger_on_ready()

    async def websocket_process(self, websocket):
        while True:
            latest = await websocket.recv()
            dd = [dbl for dbl in self.listeners if dbl.__name__ == 'on_socket_raw_receive']
            for dbl in dd: await dbl.__call__(latest)

            if latest.isdigit(): pass
            else:
                for char in latest:
                    if char.isdigit(): latest = latest.replace(char, '', 1)
                    else: break
                data = json.loads(latest)
                try: recv_type = data[0]
                except: pass
                else:
                    data = data[1]
                    ddd = [dbl_ for dbl_ in self.listeners if dbl_.__name__ == 'on_socket_cleaned_receive']
                    for dbl_ in ddd: await dbl_.__call__(data)

                    if recv_type == 'ChatMessageCreated':
                        mdata              = data['message']
                        mdata['team']      = await self.fetch_team(data['teamId'])
                        mdata['author']    = await self.fetch_user(data['createdBy'])
                        mdata['channelId'] = data['channelId']
                        message = Message(**mdata)
                        # on_message event
                        onmsg_events = [onm for onm in self.listeners if onm.__name__ == 'on_message']
                        for onm_ in onmsg_events: await onm_.__call__(message)

                        # commands
                        if message.content.startswith(self.command_prefix):
                            if message.author.id != self.user.id or message.author.id == self.owner_id:
                                # ignores self, but if the owner is itself, it does not ignore self
                                data['message'] = message
                                ctx = Context(**data)
                                ctx.invoked_command = (message.content.replace(self.command_prefix, '', 1).split(' '))[0]
                                ctx.arguments = [ctx]
                                args = message.content.replace(f'{self.command_prefix}{ctx.invoked_command}', '', 1)
                                if args != '':
                                    use_args = shlex.split(args)
                                    for a in use_args: 
                                        ctx.arguments.append(a)
                                for c in self.commands:
                                    if c.__name__ == ctx.invoked_command:
                                        try:
                                            await c(*ctx.arguments)
                                            break
                                        except: 
                                            traceback.print_exc()

                    # start typing (there is no end typing event)
                    if recv_type == 'ChatChannelTyping':
                        data['typer']     = await self.fetch_user(data['userId'])
                        event_begintyping = [l for l in self.listeners if l.__name__ == 'on_typing']
                        for type_ev in event_begintyping:
                            try:    await type_ev.__call__(data['channelId'], data['typer'], datetime.datetime.utcnow())
                            except: traceback.print_exc()

                    # delete
                    if recv_type == 'ChatMessageDeleted':
                        data['team']    = await self.fetch_team(data['teamId'])
                        data['id']      = data['message']['id']
                        #data['author'] = await self.fetch_user(data['createdBy']) 
                        # not available, see:
                        # https://www.guilded.gg/guilded-api/groups/l3GmAe9d/channels/1688bafa-9ecb-498e-9f6d-313c1cdc7150/docs/729851648
                        message         = Message(**data)
                        event_delmessage = [l for l in self.listeners if l.__name__ == 'on_message_delete']
                        for delmsg_ev in event_delmessage:
                            try:    await delmsg_ev.__call__(message)
                            except: traceback.print_exc()

                    # pin
                    if recv_type == 'ChatPinnedMessageCreated':
                        data['team']   = await self.fetch_team(data['teamId'])
                        data['id']     = data['message']['id']
                        data['author'] = await self.fetch_user(data['updatedBy'])
                        message        = Message(**data)
                        event_pinmsg   = [l for l in self.listeners if l.__name__ == 'on_pins_add']
                        for pinmsg_ev in event_pinmsg:
                            try:    await pinmsg_ev.__call__(message, data['author']) # message, who_pinned
                            except: traceback.print_exc()

                    # unpin
                    if recv_type == 'ChatPinnedMessageDeleted':
                        data['team']   = await self.fetch_team(data['teamId'])
                        data['id']     = data['message']['id']
                        data['author'] = await self.fetch_user(data['updatedBy'])
                        message        = Message(**data)
                        event_pinmsg   = [l for l in self.listeners if l.__name__ == 'on_pins_remove' or l.__name__ == 'on_unpin']
                        for pinmsg_ev in event_pinmsg:
                            try:    await pinmsg_ev.__call__(message, data['author']) # message, who_unpinned
                            except: traceback.print_exc()

                    # edited
                    if recv_type == 'ChatMessageUpdated':
                        data['team']   = await self.fetch_team(data['teamId'])
                        data['author'] = await self.fetch_user(data['updatedBy'])
                        message        = Message(**data)
                        onmsg_events   = [l for l in self.listeners if l.__name__ == 'on_message_edit']
                        for edit_ev in onmsg_events: # seems like guilded doesnt give you the previous version ://
                            try:    await edit_ev.__call__(message) 
                            except: traceback.print_exc()

    async def connect(self, cookie: str):
        websocket = await websockets.connect(WS_URL, extra_headers=[('cookie', cookie)])
        await websocket.send('2')
        await self.trigger_on_ready()
        return websocket

    async def login(self, email: str, password: str):
        if session == None: await make_session()
        loginResponse = await session.post(BASE + 'login', json={'email': email, 'password': password})
        responseJson  = (await loginResponse.json())['user']
        joinDate      = make_datetime(responseJson.pop('joinDate'))
        responseJson['joinDate'] = joinDate
        self.user = ClientUser(**responseJson)
        if self.owner_id == None:
            self.owner_id = self.user.id

        if not 'Set-Cookie' in loginResponse.headers:
            raise KeyError('Missing required information in the returned headers from Guilded. Check your credentials?')
        else:
            self.login_cookie = loginResponse.headers['Set-Cookie']

        return {'cookie': self.login_cookie, 'profile': self.user}

    async def async_run(self, email, password):
        login = await self.login(email=email, password=password)
        wsckt = await self.connect(cookie=login['cookie'])
        await asyncio.gather(
            self.websocket_process(websocket=wsckt), 
            self.heartbeat(websocket=wsckt),
            loop=self.loop)
        self.loop.run_forever()

    def run(self, email: str, password: str):
        try:
            self.loop.run_until_complete(self.async_run(email=email, password=password))
        except KeyboardInterrupt:
            #await session.close()
            #await self.loop.close()
            return

    # decorators
    def event(self, **kwargs):
        def inner_deco(func):
            return self.listeners.append(func)
        return inner_deco

    def command(self, **kwargs):
        def inner_deco(func):
            return self.commands.append(func)
        return inner_deco

class ClientUser:
    def __init__(self, *args, **kwargs):
        self.id               = kwargs.get('id')
        self.name             = kwargs.get('name')
        self.avatar_url       = kwargs.get('profilePicture')
        self.avatar_url_small = kwargs.get('profilePictureSm')
        self.avatar_url_large = kwargs.get('profilePictureLg')
        self.avatar_url_blur  = kwargs.get('profilePictureBlur')
        self.banner_url_blur  = kwargs.get('profileBannerBlur')
        self.banner_url_large = kwargs.get('profileBannerLg')
        self.steam            = kwargs.get('steamId')
        self.slug             = kwargs.get('subdomain')
        self.staffstatus      = kwargs.get('moderationstatus')
        self.info             = kwargs.get('aboutInfo')
        self.aliases          = kwargs.get('aliases')
        self.joined_at        = make_datetime(kwargs.get('joinDate'))
        self.last_online      = make_datetime(kwargs.get('lastOnline'))

class Converters:
    class MemberConverter:
        async def convert(self, ctx, to_convert):
            for member in ctx.team.members:
                if member.id == to_convert:
                    return member

class Team:
    def __init__(self, **kwargs):
        self.id                 = kwargs.get('id')
        self.type               = kwargs.get('type')
        self.created_at         = make_datetime(kwargs.get('createdAt'))
        self.owner_id           = kwargs.get('ownerId')
        self.name               = kwargs.get('name')
        self.slug               = kwargs.get('subdomain')
        self.icon_url           = kwargs.get('profilePicture')
        self.dash_image_url     = kwargs.get('teamDashImage')
        self.twitter            = kwargs['socialInfo'].get('twitter')
        self.facebook           = kwargs['socialInfo'].get('facebook')
        self.youtube            = kwargs['socialInfo'].get('youtube')
        self.twitch             = kwargs['socialInfo'].get('twitch')
        self.banner_url_small   = kwargs.get('homeBannerImageSm')
        self.banner_url_med     = kwargs.get('homeBannerImageMd')
        self.banner_url_large   = kwargs.get('homeBannerImageLg')
        self.timezone           = kwargs.get('timezone')
        self.description        = kwargs.get('description')
        self.recruiting         = kwargs.get('isRecruiting')
        self.verified           = kwargs.get('isVerified')
        self.public             = kwargs.get('isPublic')
        self.pro                = kwargs.get('isPro')
        self.sync_discord_roles = kwargs.get('autoSyncDiscordRoles')
        self.games              = kwargs.get('games')
        self.roles              = []
        for role in kwargs.get('roles'): self.roles.append(Role(**role))
        baseg = kwargs.get('baseGroup')
        baseg['team'] = self
        self.home_group         = TeamGroup(**baseg)
        self.members            = []
        for member in kwargs.get('members'): 
            member['team'] = self
            self.members.append(Member(**member))
        self.bots               = [] # :eyes:
        self.default_role       = Role(**kwargs['rolesById'].get('baseRole'))
        self.follower_count     = kwargs.get('followerCount')
        self.is_applicant       = kwargs.get('isUserApplicant') # is the bot an applicant
        self.is_following       = kwargs.get('userFollowsTeam') # is the bot following the team
        # bunch of weird stats stuff
        measurements                     = kwargs.get('measurements')
        self.member_count                = measurements.get('numMembers')
        self.recent_match_count          = measurements.get('numRecentMatches')
        self.follower_and_member_count   = measurements.get('numFollowersAndMembers')
        self.members_in_last_day_count   = measurements.get('numMembersAddedInLastDay')
        self.members_in_last_week_count  = measurements.get('numMembersAddedInLastWeek')
        self.members_in_last_month_count = measurements.get('numMembersAddedInLastMonth')
        #self.latest_member_last_online   = datetime.datetime.utcfromtimestamp(measurements.get('mostRecentMemberLastOnline'))

class TeamGroup:
    def __init__(self, **kwargs):
        self.id              = kwargs.get('id')
        self.name            = kwargs.get('name')
        self.description     = kwargs.get('description')
        self.created_at      = make_datetime(kwargs.get('createdAt'))
        self.team            = kwargs.get('team')
        self.game            = kwargs.get('gameId')
        self.role_can_see    = kwargs.get('visibilityTeamRoleId')
        self.role_is_member  = kwargs.get('membershipTeamRoleId')
        self.home            = kwargs.get('isBase')
        self.public          = kwargs.get('isPublic')

class abc:
    class Messageable(metaclass=abc.ABCMeta):
        def __init__(self):
            self.channel = self.channel_id

        async def fetch_message(id):
            message = await session.get(BASE + 'content/route/metadata?route=//channels/'+ self.channel +'/chat?messageId='+ id)
            message = (await message.json())['metadata']
            message['author'] = await Bot.fetch_user(message['createdBy'])
            return Message(**message)

        async def send(self, content=None, embed=None):
            '''Send a message to a channel'''
            rand_uuid = str(uuid.uuid1())
            post_json = {
                "messageId": rand_uuid,
                "content": {
                    "object": "value",
                    "document": {
                        "object":"document",
                        "data": {},
                        "nodes": []
                    }
                }
            }
            if content != None:
                post_json['content']['document']['nodes'].append({
                    "object": "block",
                    "type": "markdown-plain-text",
                    "data": {},
                    "nodes": [{
                        "object":"text",
                        "leaves": [{
                            "object": "leaf",
                            "text": content,
                            "marks": []
                        }]
                    }]
                })
            if embed != None:
                post_json['content']['document']['nodes'].append({
                    "object": "block",
                    "type": "webhookMessage",
                    "data": {'embeds': [embed.default]},
                    "nodes": []})

            if content == None and embed == None:
                raise ValueError('content and embed cannot both be None.')

            # POST the message to the channel
            msg = await session.post(BASE + 'channels/' + self.channel + '/messages', json=post_json)
            #msg = await msg.json()
            return 200
            #return Message(**msg['message'])

    class User(Messageable, metaclass=abc.ABCMeta):
        def __init__(self):
            self.name         = self.name 
            self.id           = self.id
            self.display_name = self.display_name or self.name

class User(abc.User):
    def __init__(self, **kwargs):
        self.id           = kwargs.get('id')
        self.name         = kwargs.get('name')
        self.display_name = self.name
        self.about        = kwargs.get('aboutInfo')
        self.slug         = kwargs.get('subdomain')
        self.steam        = kwargs.get('steamId')
        self.last_online  = make_datetime(kwargs.get('lastOnline'))
        self.created_at   = make_datetime(kwargs.get('joinDate'))

class Member(abc.User):
    def __init__(self, **kwargs):
        self.id          = kwargs.get('id')
        self.name        = kwargs.get('name')
        self.team        = kwargs.get('team')
        self.nick        = kwargs.get('nickname')
        if self.nick == None: self.display_name = self.name
        else:                 self.display_name = self.nick
        self.xp          = kwargs.get('teamXp')
        self.last_online = make_datetime(kwargs.get('lastOnline'))
        self.created_at  = make_datetime(kwargs.get('joinDate'))

    async def edit(self, nick=None):
        if nick != None:
            await session.put(
                BASE + 'teams/' + self.team.id + '/members/' + self.id + '/nickname',
                json={'nickname': nick})

class Role:
    def __init__(self, **kwargs):
        self.id                  = kwargs.get('id')     # an int :o
        self.name                = kwargs.get('name')
        self.color               = kwargs.get('color')  # hexval
        self.is_default          = kwargs.get('isBase') # is it the default member role (i think?)
        self.team                = kwargs.get('teamId')
        self.created_at          = make_datetime(kwargs.get('createdAt'))
        self.updated_at          = make_datetime(kwargs.get('updatedAt'))
        self.mentionable         = kwargs.get('isMentionable')
        self.discord_id          = kwargs.get('discordRoleId')
        self.self_assignable     = kwargs.get('isSelfAssignable')
        self.discord_last_synced = kwargs.get('discordSyncedAt')
        if self.discord_last_synced != None: self.discord_last_synced = make_datetime(self.discord_last_synced)

class Embed:
    def __init__(self, title=None, description=None, color=None, url=None):
        # Timestamps are currently unsupported I'm sorry
        # I'll get off my lazy butt and do it someday
        # Stupid timezones
        self.default = {
            "title": title,
            "description": description,
            "color": color,
            "url": url,
            "author": {
                "name": None,
                "url": None,
                "icon_url": None
            },
            "footer": {
                "text": None,
                "icon_url": None
            },
            "image": {
                "url": None
            },
            "thumbnail": {
                "url": None
            }
        }

    def set_author(self, name: str, url: str = None, icon_url: str = None):
        self.default['author'] = {'name': name, 'url': url, 'icon_url': icon_url}

    def set_footer(self, text: str, icon_url: str = None):
        self.default['footer'] = {'text': text, 'icon_url': icon_url}

    def set_image(self, url: str):
        self.default['image'] = {'url': url}

    def set_thumbnail(self, url: str):
        self.default['thumbnail'] = {'url': url}

class TextChannel(abc.Messageable):
    def __init__(self, **kwargs):
        self.id         = kwargs.get('id')
        self.type       = kwargs.get('type')
        self.created_at = make_datetime(kwargs.get('createdAt'))
        self.updated_at = make_datetime(kwargs.get('updatedAt'))
        self.created_by = kwargs.get('createdBy')
        self.channel_id = self.id

class Message:
    def __init__(self, **kwargs):
        self.channel    = kwargs.get('channelId')
        self.team       = kwargs.get('team')
        self.created_at = make_datetime(kwargs.get('createdAt'))
        self.id         = kwargs.get('id')
        self.author     = kwargs.get('author')
        self.content    = ''
        content0        = kwargs['content']['document']['nodes'][0]['nodes']
        for aaaHelpMe  in content0:
            cont_append   = aaaHelpMe['leaves'][0]['text']
            self.content += cont_append

    async def add_reaction(emoji_id):
        react = await session.post(BASE + 'channels/' + self.channel + '/messages/' + self.id + '/reactions/' + emoji_id)
        return await react.json()

class Context(abc.Messageable):
    def __init__(self, **kwargs):
        message = kwargs.get('message')
        self.message         = message
        self.author          = message.author
        self.channel         = message.channel
        self.content         = message.content
        self.team            = message.team
        self.invoked_command = None
        self.arguments       = []
        self.channel_id = self.channel
