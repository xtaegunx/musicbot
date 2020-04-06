# -*- coding: utf-8 -*- 

import os
import discord
from discord.ext import commands
import logging
import asyncio
import itertools
import sys
import traceback
import random
from async_timeout import timeout
from functools import partial
from youtube_dl import YoutubeDL
from io import StringIO

##################### 로깅 ###########################
log_stream = StringIO()    
logging.basicConfig(stream=log_stream, level=logging.WARNING)

#ilsanglog = logging.getLogger('discord')
#ilsanglog.setLevel(level = logging.WARNING)
#handler = logging.StreamHandler()
#handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
#ilsanglog.addHandler(handler)
#####################################################

access_token = os.environ["BOT_TOKEN"]	

def init():
	global command

	command = []
	fc = []

	command_inidata = open('command.ini', 'r', encoding = 'utf-8')
	command_inputData = command_inidata.readlines()

	############## 뮤직봇 명령어 리스트 #####################
	for i in range(len(command_inputData)):
		tmp_command = command_inputData[i][12:].rstrip('\n')
		fc = tmp_command.split(', ')
		command.append(fc)
		fc = []
		#command.append(command_inputData[i][12:].rstrip('\n'))     #command[0] ~ [28] : 명령어

	del command[0]

	command_inidata.close()

	#print (command)

init()

ytdlopts = {
	'format': 'bestaudio/best',
	'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
	'restrictfilenames': True,
	'noplaylist': True,
	'nocheckcertificate': True,
	'ignoreerrors': False,
	'logtostderr': False,
	'quiet': True,
	'no_warnings': True,
	'default_search': 'auto',
	'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
	'before_options': '-nostdin',
	'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
	"""Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
	"""Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):

	def __init__(self, source, *, data, requester):
		super().__init__(source)
		self.requester = requester

		self.title = data.get('title')
		self.web_url = data.get('webpage_url')

	def __getitem__(self, item: str):
		"""Allows us to access attributes similar to a dict.
		This is only useful when you are NOT downloading.
		"""
		return self.__getattribute__(item)

	@classmethod
	async def create_source(cls, ctx, search: str, *, loop, download=False):
		loop = loop or asyncio.get_event_loop()

		to_run = partial(ytdl.extract_info, url=search, download=download)
		data = await loop.run_in_executor(None, to_run)

		if 'entries' in data:
			# take first item from a playlist
			data = data['entries'][0]

		await ctx.send(f'```ini\n[Added {data["title"]} to the Queue.]\n```', delete_after=15)

		if download:
			source = ytdl.prepare_filename(data)
		else:
			return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

		return cls(discord.FFmpegPCMAudio(source,before_options=" -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"), data=data, requester=ctx.author)

	@classmethod
	async def regather_stream(cls, data, *, loop):
		"""Used for preparing a stream, instead of downloading.
		Since Youtube Streaming links expire."""
		loop = loop or asyncio.get_event_loop()
		requester = data['requester']

		to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
		data = await loop.run_in_executor(None, to_run)

		return cls(discord.FFmpegPCMAudio(data['url'], before_options=" -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"), data=data, requester=requester)


class MusicPlayer:
	"""A class which is assigned to each guild using the bot for Music.
	This class implements a queue and loop, which allows for different guilds to listen to different playlists
	simultaneously.
	When the bot disconnects from the Voice it's instance will be destroyed.
	"""

	__slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

	def __init__(self, ctx):
		self.bot = ctx.bot
		self._guild = ctx.guild
		self._channel = ctx.channel
		self._cog = ctx.cog

		self.queue = asyncio.Queue()
		self.next = asyncio.Event()

		self.np = None  # Now playing message
		self.volume = .5
		self.current = None

		ctx.bot.loop.create_task(self.player_loop())

	async def player_loop(self):
		"""Our main player loop."""
		await self.bot.wait_until_ready()

		while not self.bot.is_closed():
			self.next.clear()

			try:
				# Wait for the next song. If we timeout cancel the player and disconnect...
				async with timeout(300):  # 5 minutes...
					source = await self.queue.get()
			except asyncio.TimeoutError:
				return self.destroy(self._guild)

			if not isinstance(source, YTDLSource):
				# Source was probably a stream (not downloaded)
				# So we should regather to prevent stream expiration
				try:
					source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
				except Exception as e:
					await self._channel.send(f'There was an error processing your song.\n'
											f'```css\n[{e}]\n```')
					continue

			source.volume = self.volume
			self.current = source

			self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
			self.np = await self._channel.send(f'**Now Playing : **  `{source.title}`  requested by  ' f'`{source.requester}`')
			await self.next.wait()

			# Make sure the FFmpeg process is cleaned up.
			source.cleanup()
			self.current = None

			try:
				# We are no longer playing this song...
				await self.np.delete()
			except discord.HTTPException:
				pass

	def destroy(self, guild):
		"""Disconnect and cleanup the player."""
		return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
	"""Music related commands."""

	__slots__ = ('bot', 'players')

	def __init__(self, bot):
		self.bot = bot
		self.players = {}

	async def cleanup(self, guild):
		try:
			await guild.voice_client.disconnect()
		except AttributeError:
			pass

		try:
			del self.players[guild.id]
		except KeyError:
			pass

	async def __local_check(self, ctx):
		"""A local check which applies to all commands in this cog."""
		if not ctx.guild:
			raise commands.NoPrivateMessage
		return True

	async def __error(self, ctx, error):
		"""A local error handler for all errors arising from commands in this cog."""
		if isinstance(error, commands.NoPrivateMessage):
			try:
				return await ctx.send('This command can not be used in Private Messages.')
			except discord.HTTPException:
				pass
		elif isinstance(error, InvalidVoiceChannel):
			await ctx.send('Error connecting to Voice Channel. '
						'Please make sure you are in a valid channel or provide me with one')

		print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
		traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

	def get_player(self, ctx):
		"""Retrieve the guild player, or generate one."""
		try:
			player = self.players[ctx.guild.id]
		except KeyError:
			player = MusicPlayer(ctx)
			self.players[ctx.guild.id] = player

		return player

	#@commands.command(name='!connect', aliases=['join'])   #채널 접속
	@commands.command(name='!connect', aliases=command[0])   #채널 접속
	async def connect_(self, ctx, *, channel: discord.VoiceChannel=None):
		"""Connect to voice.
		Parameters
		------------
		channel: discord.VoiceChannel [Optional]
			The channel to connect to. If a channel is not specified, an attempt to join the voice channel you are in
			will be made.
		This command also handles moving the bot to different channels.
		"""
		if not channel:
			try:
				channel = ctx.author.voice.channel
			except AttributeError:
				await ctx.send(':no_entry_sign: 음성채널에 접속하고 사용해주세요.', delete_after=20)
				raise InvalidVoiceChannel(':no_entry_sign: 음성채널에 접속하고 사용해주세요.')

		vc = ctx.voice_client

		if vc:
			if vc.channel.id == channel.id:
				return
			try:
				await vc.move_to(channel)
			except asyncio.TimeoutError:
				await ctx.send(f':no_entry_sign: 채널 이동 : <{channel}> 시간 초과.', delete_after=20)
				raise VoiceConnectionError(f':no_entry_sign: 채널 이동 : <{channel}> 시간 초과.')
		else:
			try:
				await channel.connect()
			except asyncio.TimeoutError:
				await ctx.send(f':no_entry_sign: 채널 접속 : <{channel}> 시간 초과.', delete_after=20)
				raise VoiceConnectionError(f':no_entry_sign: 채널 접속: <{channel}> 시간 초과.')

		await ctx.send(f'Connected to : **{channel}**', delete_after=20)

	#@commands.command(name='!play', aliases=['sing'])     #재생
	@commands.command(name='!play', aliases=command[1])     #재생
	async def play_(self, ctx, *, search: str):
		"""Request a song and add it to the queue.
		This command attempts to join a valid voice channel if the bot is not already in one.
		Uses YTDL to automatically search and retrieve a song.
		Parameters
		------------
		search: str [Required]
			The song to search and retrieve using YTDL. This could be a simple search, an ID or URL.
		"""
		await ctx.trigger_typing()

		vc = ctx.voice_client

		if not vc:
			await ctx.invoke(self.connect_)

		player = self.get_player(ctx)

		# If download is False, source will be a dict which will be used later to regather the stream.
		# If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
		source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

		await player.queue.put(source)

	#@commands.command(name='!pause')    #일시정지
	@commands.command(name='!pause', aliases=command[2])    #일시정지
	async def pause_(self, ctx):
		"""Pause the currently playing song."""
		vc = ctx.voice_client

		if not vc or not vc.is_playing():
			return await ctx.send(':mute: 현재 재생중인 음악이 없습니다.', delete_after=20)
		elif vc.is_paused():
			return

		vc.pause()
		await ctx.send(f'**`{ctx.author}`**: 음악 정지!')

	#@commands.command(name='!resume')   #다시재생
	@commands.command(name='!resume', aliases=command[3])   #다시재생
	async def resume_(self, ctx):
		"""Resume the currently paused song."""
		vc = ctx.voice_client

		if not vc or not vc.is_connected():
			return await ctx.send(':mute: 현재 재생중인 음악이 없습니다.', delete_after=20)
		elif not vc.is_paused():
			return

		vc.resume()
		await ctx.send(f'**`{ctx.author}`**: 음악 다시 재생!')

	#@commands.command(name='!skip')   #스킵
	@commands.command(name='!skip', aliases=command[4])   #스킵
	async def skip_(self, ctx):
		"""Skip the song."""
		vc = ctx.voice_client

		if not vc or not vc.is_connected():
			return await ctx.send(':mute: 현재 재생중인 음악이 없습니다.', delete_after=20)

		if vc.is_paused():
			pass
		elif not vc.is_playing():
			return

		vc.stop()
		await ctx.send(f'**`{ctx.author}`**: 음악 스킵!')

	#@commands.command(name='!queue', aliases=['q', 'playlist'])   #재생목록
	@commands.command(name='!queue', aliases=command[5])   #재생목록
	async def queue_info(self, ctx):
		"""Retrieve a basic queue of upcoming songs."""
		vc = ctx.voice_client

		if not vc or not vc.is_connected():
			return await ctx.send(':mute: 현재 재생중인 음악이 없습니다.', delete_after=20)

		player = self.get_player(ctx)
		if player.queue.empty():
			return await ctx.send(':mute: 더 이상 재생할 곡이 없습니다.')

		# Grab up to 5 entries from the queue...
		upcoming = list(itertools.islice(player.queue._queue, 0, 5))

		fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
		embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

		await ctx.send(embed=embed)

	#@commands.command(name='!now_playing', aliases=['np', 'current', 'currentsong', 'playing'])   #현재 재생음악
	@commands.command(name='!now_playing', aliases=command[6])   #현재 재생음악
	async def now_playing_(self, ctx):
		"""Display information about the currently playing song."""
		vc = ctx.voice_client

		if not vc or not vc.is_connected():
			return await ctx.send(':no_entry_sign: 현재 접속중인 음악채널이 없습니다.', delete_after=20)

		player = self.get_player(ctx)
		if not player.current:
			return await ctx.send(':mute: 현재 재생중인 음악이 없습니다.')

		try:
			# Remove our previous now_playing message.
			await player.np.delete()
		except discord.HTTPException:
			pass

		player.np = await ctx.send(f'**Now Playing : ** `{vc.source.title}` 'f'  requested by  `{vc.source.requester}`')

	#@commands.command(name='!volume', aliases=['vol'])   #볼륨조정
	@commands.command(name='!volume', aliases=command[7])   #볼륨조정
	async def change_volume(self, ctx, *, vol: float):
		"""Change the player volume.
		Parameters
		------------
		volume: float or int [Required]
			The volume to set the player to in percentage. This must be between 1 and 100.
		"""
		vc = ctx.voice_client

		if not vc or not vc.is_connected():
			return await ctx.send(':no_entry_sign: 현재 접속중인 음악채널이 없습니다.', delete_after=20)

		if not 0 < vol < 101:
			return await ctx.send('볼륨은 1 ~ 100 사이로 입력 해주세요.')

		player = self.get_player(ctx)

		if vc.source:
			vc.source.volume = vol / 100

		player.volume = vol / 100
		await ctx.send(f'**`{ctx.author}`**: 님이 볼륨을 **{vol}%** 로 조정하였습니다.')

	#@commands.command(name='stop')   #정지
	@commands.command(name='!stop', aliases=command[8])   #정지
	async def stop_(self, ctx):
		"""Stop the currently playing song and destroy the player.
		!Warning!
			This will destroy the player assigned to your guild, also deleting any queued songs and settings.
		"""
		vc = ctx.voice_client

		if not vc or not vc.is_connected():
			return await ctx.send(':no_entry_sign: 현재 접속중인 음악채널이 없습니다.', delete_after=20)

		await self.cleanup(ctx.guild)

	@commands.command(name='!race', aliases=command[9])   #경주
	async def modify_(self, ctx, *, msg: str):
		race_info = []
		fr = []
		racing_field = []
		str_racing_field = []
		cur_pos = []
		race_val = []
		random_pos = []
		racing_result = []
		output = ':camera: :camera: :camera: 신나는 레이싱! :camera: :camera: :camera:\n'
		#racing_unit = [':giraffe:', ':elephant:', ':tiger2:', ':hippopotamus:', ':crocodile:',':leopard:',':ox:', ':sheep:', ':pig2:',':dromedary_camel:',':dragon:',':rabbit2:'] #동물스킨
		racing_unit = [':red_car:', ':taxi:', ':bus:', ':trolleybus:', ':race_car:', ':police_car:', ':ambulance:', ':fire_engine:', ':minibus:', ':truck:', ':articulated_lorry:', ':tractor:', ':scooter:', ':manual_wheelchair:', ':motor_scooter:', ':auto_rickshaw:', ':blue_car:', ':bike:', ':helicopter:', ':steam_locomotive:']  #탈것스킨
		random.shuffle(racing_unit) 
		racing_member = msg.split(" ")

		if racing_member[0] == "종료" :
			await ctx.send('경주 종료!')
			return
		elif racing_member[0] == "입장" :
			if len(racing_member) == 2:
				await ctx.send('레이스 인원이 1명 입니다.')
				return
			elif len(racing_member) >= 14:
				await ctx.send('레이스 인원이 12명 초과입니다.')
				return
			else :
				race_val = random.sample(range(14, 14+len(racing_member)-1), len(racing_member)-1)
				for i in range(len(racing_member)-1):
					fr.append(racing_member[i+1])
					fr.append(racing_unit[i])
					fr.append(race_val[i])
					race_info.append(fr)
					fr = []
					for i in range(66):
						fr.append(" ")
					racing_field.append(fr)
					fr = []

				for i in range(len(racing_member)-1):
					racing_field[i][0] = "|"
					racing_field[i][64] = race_info[i][1]
					racing_field[i][65] = "| " + race_info[i][0]
					str_racing_field.append("".join(racing_field[i]))
					cur_pos.append(64)

				for i in range(len(racing_member)-1):
					output +=  str_racing_field[i] + '\n'
					
				
				result_race = await ctx.send(output + ':traffic_light: 3초 후 경주가 시작됩니다!')
				await asyncio.sleep(1)
				await result_race.edit(content = output + ':traffic_light: 2초 후 경주가 시작됩니다!')
				await asyncio.sleep(1)
				await result_race.edit(content = output + ':traffic_light: 1초 후 경주가 시작됩니다!')
				await asyncio.sleep(1)
				await result_race.edit(content = output + ':checkered_flag:  경주 시작!')								

				for i in range(len(racing_member)-1):
					test = random.sample(range(2,64), race_info[i][2])
					while len(test) != 14 + len(racing_member)-2 :
						test.append(1)
					test.append(1)
					test.sort(reverse=True)
					random_pos.append(test)
				
				for j in range(len(random_pos[0])):
					if j%2 == 0:
						output =  ':camera: :camera_with_flash: :camera: 신나는 레이싱! :camera_with_flash: :camera: :camera_with_flash:\n'
					else :
						output =  ':camera_with_flash: :camera: :camera_with_flash: 신나는 레이싱! :camera: :camera_with_flash: :camera:\n'
					str_racing_field = []
					for i in range(len(racing_member)-1):
						temp_pos = cur_pos[i]
						racing_field[i][random_pos[i][j]], racing_field[i][temp_pos] = racing_field[i][temp_pos], racing_field[i][random_pos[i][j]]
						cur_pos[i] = random_pos[i][j]
						str_racing_field.append("".join(racing_field[i]))

					await asyncio.sleep(1) 

					for i in range(len(racing_member)-1):
						output +=  str_racing_field[i] + '\n'
					
					await result_race.edit(content = output + ':checkered_flag:  경주 시작!')
				
				for i in range(len(racing_field)):
					fr.append(race_info[i][0])
					fr.append((race_info[i][2])-13)
					racing_result.append(fr)
					fr = []

				result = sorted(racing_result, key=lambda x: x[1])

				result_str = ''
				for i in range(len(result)):
					if result[i][1] == 1:
						result[i][1] = ':first_place:'
					elif result[i][1] == 2:
						result[i][1] = ':second_place:'
					elif result[i][1] == 3:
						result[i][1] = ':third_place:'
					elif result[i][1] == 4:
						result[i][1] = ':four:'
					elif result[i][1] == 5:
						result[i][1] = ':five:'
					elif result[i][1] == 6:
						result[i][1] = ':six:'
					elif result[i][1] == 7:
						result[i][1] = ':seven:'
					elif result[i][1] == 8:
						result[i][1] = ':eight:'
					elif result[i][1] == 9:
						result[i][1] = ':nine:'
					elif result[i][1] == 10:
						result[i][1] = ':keycap_ten:'
					elif result[i][1] == 11:
						result[i][1] = ':x:'
					elif result[i][1] == 12:
						result[i][1] = ':x:'
					result_str += result[i][1] + "  " + result[i][0] + "  "
					
				#print(result)
					
				await result_race.edit(content = output + ':tada: 경주 종료!\n' + result_str)

bot = commands.Bot(command_prefix=commands.when_mentioned_or(""),description='일상뮤직봇')

@bot.event
async def on_ready():
	print("Logged in as ") #화면에 봇의 아이디, 닉네임이 출력됩니다.
	print(bot.user.name)
	print(bot.user.id)
	print("===========")

bot.add_cog(Music(bot))
bot.run(access_token)

