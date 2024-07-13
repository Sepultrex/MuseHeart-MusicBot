# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import pickle
import re
import traceback
from base64 import b64decode, b64encode
from copy import deepcopy
from io import BytesIO
from itertools import islice
from typing import List, Union, Optional, TYPE_CHECKING, Literal

import disnake
from disnake.ext import commands

from utils.db import DBModel
from utils.music.audio_sources.deezer import deezer_regex
from utils.music.audio_sources.spotify import spotify_regex_w_user
from utils.music.checks import check_pool_bots
from utils.music.converters import time_format, fix_characters, URL_REG
from utils.music.errors import GenericError
from utils.music.models import LavalinkPlayer, LavalinkTrack
from utils.music.skin_utils import skin_converter
from utils.others import check_cmd, CustomContext, send_idle_embed, music_source_emoji_url, \
    PlayerControls, get_source_emoji_cfg

if TYPE_CHECKING:
    from utils.client import BotCore


class VolumeInteraction(disnake.ui.View):

    def __init__(self, inter):
        self.inter = inter
        self.volume = None
        super().__init__(timeout=30)
        self.process_buttons()

    def process_buttons(self):

        opts = []

        for l in [5, 20, 40, 60, 80, 100, 120, 150]:

            if l > 100:
                description = "%100'ün üzerinde ses çok kötü olabilir."
            else:
                description = None
            opts.append(disnake.SelectOption(label=f"{l}%", value=f"vol_{l}", description=description))

        select = disnake.ui.Select(placeholder='Seviye:', options=opts)
        select.callback = self.callback
        self.add_item(select)

    async def callback(self, interaction: disnake.MessageInteraction):
        await interaction.response.edit_message(content=f"Ses seviyesi değişti!",embed=None, view=None)
        self.volume = int(interaction.data.values[0][4:])
        self.stop()


class QueueInteraction(disnake.ui.View):

    def __init__(self, bot: BotCore, user: disnake.Member, timeout=120):

        self.bot = bot
        self.user = user
        self.track_pages = []
        self.select_options = []
        self.current_page = 0
        self.max_page = 1
        self.max_items = 8
        self.message: Optional[disnake.Message] = None
        self.current_track: Optional[LavalinkTrack] = None
        super().__init__(timeout=timeout)
        self.embed = disnake.Embed(color=self.bot.get_color(user.guild.me))
        self.update_pages()
        self.update_embed()

    def update_pages(self, reset_page=True):

        player: LavalinkPlayer = self.bot.music.players[self.user.guild.id]

        if reset_page:
            self.current_page = 0

        self.track_pages.clear()
        self.track_pages = list(disnake.utils.as_chunks(player.queue or player.queue_autoplay, max_size=self.max_items))
        self.current_track = self.track_pages[self.current_page][0]
        self.max_page = len(self.track_pages) - 1
        self.update_components()

    async def on_timeout(self) -> None:

        if not self.message:
            return

        self.embed.set_footer(text="Etkileşim zamanı bitti!")

        for c in self.children:
            c.disabled = True

        await self.message.edit(embed=self.embed, view=self)


    def update_components(self):

        if not self.select_options:
            return

        self.clear_items()

        track_select = disnake.ui.Select(
            placeholder="Sayfadan belirli bir şarkıyı çal:",
            options=self.select_options,
            custom_id="queue_track_selection",
            max_values=1
        )

        track_select.callback = self.track_select_callback

        self.add_item(track_select)

        first = disnake.ui.Button(emoji='⏮️', style=disnake.ButtonStyle.grey)
        first.callback = self.first
        self.add_item(first)

        back = disnake.ui.Button(emoji='⬅️', style=disnake.ButtonStyle.grey)
        back.callback = self.back
        self.add_item(back)

        next = disnake.ui.Button(emoji='➡️', style=disnake.ButtonStyle.grey)
        next.callback = self.next
        self.add_item(next)

        last = disnake.ui.Button(emoji='⏭️', style=disnake.ButtonStyle.grey)
        last.callback = self.last
        self.add_item(last)

        stop_interaction = disnake.ui.Button(emoji='⏹️', style=disnake.ButtonStyle.grey)
        stop_interaction.callback = self.stop_interaction
        self.add_item(stop_interaction)

        play = disnake.ui.Button(emoji='▶️', label="Başlat", style=disnake.ButtonStyle.grey, custom_id="queue_skip")
        play.callback = self.invoke_command
        self.add_item(play)

        move = disnake.ui.Button(emoji="↪️", label="Taşıyıcı", style=disnake.ButtonStyle.grey, custom_id="queue_move")
        move.callback = self.move_callback
        self.add_item(move)

        rotate_q = disnake.ui.Button(emoji='🔃', label="Sırayı Döndür", style=disnake.ButtonStyle.grey, custom_id="queue_rotate")
        rotate_q.callback = self.invoke_command
        self.add_item(rotate_q)

        update_q = disnake.ui.Button(emoji='🔄', label="Yeniden Ekle", style=disnake.ButtonStyle.grey)
        update_q.callback = self.update_q
        self.add_item(update_q)

    async def move_callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(
            ViewModal(
                view=self, title="Seçilen şarkıyı taşı", custom_id="queue_move_modal",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Kuyruk konumu:",
                        custom_id="queue_move_position",
                        max_length=4,
                        required=True
                    ),
                ]
            )
        )

    async def modal_handler(self, inter: disnake.ModalInteraction):

        try:
            if inter.data.custom_id == "queue_move_modal":

                if not inter.text_values["queue_move_position"].isdigit():
                    await inter.send("Geçerli bir numara kullanmalısınız...", ephemeral=True)
                    return

                await check_cmd(self.bot.get_slash_command("move"), inter)

                player: LavalinkPlayer = self.bot.music.players[self.user.guild.id]

                move_func = None

                try:
                    player.queue.remove(self.current_track)
                    move_func = player.queue.insert
                except ValueError:
                    try:
                        player.queue_autoplay.remove(self.current_track)
                        move_func = player.queue_autoplay.insert
                    except ValueError:
                        pass

                if move_func:
                    move_func((int(inter.text_values["queue_move_position"]) or 1)-1, self.current_track)
                    player.update = True

                self.update_pages(reset_page=False)

                if self.current_page > self.max_page:
                    self.current_page = 0

                self.update_embed()

                if self.message:
                    await inter.response.edit_message(embed=self.embed, view=self)
                else:
                    await inter.edit_original_message(embed=self.embed, view=self)

            else:
                await inter.send(f"Henüz uygulanmayan yöntem: {inter.data.custom_id}", ephemeral=True)

        except Exception as e:
            self.bot.dispatch('interaction_player_error', inter, e)


    def update_embed(self):

        self.embed.title = f"**Sıradaki şarkılar [Sayfa: {self.current_page+1} / {self.max_page+1}]**"

        opts = []

        txt = ""

        for n, t in enumerate(self.track_pages[self.current_page]):

            duration = time_format(t.duration) if not t.is_stream else '🔴 Canlı yayın'

            index = (self.max_items*self.current_page) + n + 1

            if self.current_track == t:
                txt += f"`╔{'='*50}`\n`║` **{index}º) [{fix_characters(t.title, limit=37)}]({t.uri})**\n" \
                       f"`║ ⏲️`  **{duration}**" + (f" - `Temsilciler: {t.track_loops}`" if t.track_loops else "") + \
                       " **|** " + (f"`✋` <@{t.requester}>" if not t.autoplay else f"`👍⠂Tavsiye edilen`") + f"\n`╚{'='*50}`\n"
            else:
                txt += f"`┌ {index})` [`{fix_characters(t.title, limit=45)}`]({t.uri})\n" \
                       f"`└ ⏲️ {duration}`" + (f" - `Temsilciler: {t.track_loops}`" if t.track_loops else "") + \
                       f" **|** " + (f"`✋` <@{t.requester}>" if not t.autoplay else f"`👍⠂Tavsiye edilen`") + "\n"

            opts.append(
                disnake.SelectOption(
                    label=f"{index}. {t.author}"[:25], description=f"[{duration}] | {t.title}"[:50],
                    value=f"queue_select_{t.unique_id}", default=t == self.current_track
                )
            )

        self.embed.description = txt
        self.select_options = opts
        self.embed.set_thumbnail(self.current_track.thumb)
        self.update_components()

    async def track_select_callback(self, interaction: disnake.MessageInteraction):

        track_id = interaction.values[0][13:]

        track = None

        try:
            player: LavalinkPlayer = self.bot.music.players[self.user.guild.id]
        except KeyError:
            self.stop()
            return

        for t in  player.queue + player.queue_autoplay:
            if t.unique_id == track_id:
                track = t
                break

        if not track:
            await interaction.send(f"\"{track_id}\" kimliğine sahip şarkı oynatıcı kuyruğunda bulunamadı..", ephemeral=True)
            return

        self.current_track = track
        self.update_embed()

        if self.message:
            await interaction.response.edit_message(embed=self.embed, view=self)
        else:
            await interaction.edit_original_message(embed=self.embed, view=self)

    async def invoke_command(self, interaction: disnake.MessageInteraction):

        try:
            player = self.bot.music.players[self.user.guild.id]
        except KeyError:
            await interaction.send("Oyuncu zaten kesinleşti...", ephemeral=True)
            self.stop()
            return

        update_inter = False

        try:
            if self.current_track is None:
                await interaction.send("Hiçbir şarkı seçilmedi...", ephemeral=True)
                return

            if interaction.data.custom_id == "queue_skip":
                if player.current and player.current.unique_id == self.current_track.unique_id:
                    await check_cmd(self.bot.get_slash_command("seek"), interaction)
                    await player.seek(0)
                    player.set_command_log(emoji="⏪", text=f"{interaction.author.mention} şarkıdan geriye doğru taşındı: `0:00`")
                    player.update = True
                    await interaction.response.defer()
                    return
                else:
                    command = self.bot.get_slash_command("skip")
                    kwargs = {"query": f"{self.current_track.title} || ID > {self.current_track.unique_id}"}

            elif interaction.data.custom_id == "queue_rotate":
                command = self.bot.get_slash_command("rotate")
                kwargs = {"query": f"{self.current_track.title} || ID > {self.current_track.unique_id}"}
                update_inter = True

            else:
                await interaction.send(f"Komut uygulanmadı: {interaction.data.custom_id}", ephemeral=True)
                return

            interaction.music_bot = self.bot
            interaction.music_guild = self.user.guild

            await check_cmd(command, interaction)
            await command(interaction, **kwargs)

            if update_inter:
                self.update_pages()
                self.update_embed()
                if self.message:
                    await interaction.response.edit_message(embed=self.embed, view=self)
                else:
                    await interaction.edit_original_message(embed=self.embed, view=self)

        except Exception as e:
            self.bot.dispatch('interaction_player_error', interaction, e)

    async def first(self, interaction: disnake.MessageInteraction):

        self.current_page = 0
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def back(self, interaction: disnake.MessageInteraction):

        if self.current_page == 0:
            self.current_page = self.max_page
        else:
            self.current_page -= 1
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def next(self, interaction: disnake.MessageInteraction):

        if self.current_page == self.max_page:
            self.current_page = 0
        else:
            self.current_page += 1
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def last(self, interaction: disnake.MessageInteraction):

        self.current_page = self.max_page
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)


    async def stop_interaction(self, interaction: disnake.MessageInteraction):

        await interaction.response.edit_message(content="Kapalı kuyruk", embed=None, view=None)
        self.stop()

    async def update_q(self, interaction: disnake.MessageInteraction):

        self.current_page = 0
        self.max_page = len(self.track_pages) - 1
        self.update_pages()
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def interaction_check(self, interaction: disnake.MessageInteraction):

        if interaction.author != self.user:
            await interaction.send(f"Burada yalnızca {self.user.mention} üyesi etkileşim kurabilir.", ephemeral=True)
            return

        try:
            self.bot.music.players[self.user.guild.id]
        except KeyError:
            await interaction.response.edit_message(content="Oynatıcının işi bitti...", embed=None, view=None)
            self.stop()
            return

        return True

class SelectInteraction(disnake.ui.View):

    def __init__(self, user: disnake.Member, opts: List[disnake.SelectOption], *, timeout=180):
        super().__init__(timeout=timeout)
        self.user = user
        self.selected = None
        self.item_pages = list(disnake.utils.as_chunks(opts, 25))
        self.current_page = 0
        self.max_page = len(self.item_pages)-1
        self.inter = None

        self.load_components()

    def load_components(self):

        self.clear_items()

        select_menu = disnake.ui.Select(placeholder='Bir seçenek seçin:', options=self.item_pages[self.current_page])
        select_menu.callback = self.callback
        self.add_item(select_menu)
        self.selected = self.item_pages[self.current_page][0].value

        if len(self.item_pages) > 1:

            back_button = disnake.ui.Button(emoji="⬅")
            back_button.callback = self.back_callback
            self.add_item(back_button)

            next_button = disnake.ui.Button(emoji="➡")
            next_button.callback = self.next_callback
            self.add_item(next_button)

        button = disnake.ui.Button(label="İptal etmek", emoji="❌")
        button.callback = self.cancel_callback
        self.add_item(button)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:

        if interaction.user.id == self.user.id:
            return True

        await interaction.send(f"Burada yalnızca {self.user.mention} etkileşim kurabilir.", ephemeral = True)

    async def back_callback(self, interaction: disnake.MessageInteraction):
        if self.current_page == 0:
            self.current_page = self.max_page
        else:
            self.current_page -= 1
        self.load_components()
        await interaction.response.edit_message(view=self)

    async def next_callback(self, interaction: disnake.MessageInteraction):
        if self.current_page == self.max_page:
            self.current_page = 0
        else:
            self.current_page += 1
        self.load_components()
        await interaction.response.edit_message(view=self)

    async def cancel_callback(self, interaction: disnake.MessageInteraction):
        self.selected = False
        self.inter = interaction
        self.stop()

    async def callback(self, interaction: disnake.MessageInteraction):
        self.selected = interaction.data.values[0]
        self.inter = interaction
        self.stop()


class AskView(disnake.ui.View):

    def __init__(self, *, ctx: Union[commands.Context, disnake.Interaction], timeout=None):
        super().__init__(timeout=timeout)
        self.selected = None
        self.ctx = ctx
        self.interaction_resp: Optional[disnake.MessageInteraction] = None

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:

        if interaction.user != self.ctx.author:
            await interaction.send("Bu düğmeyi kullanamazsınız!", ephemeral=True)
            return False

        return True

    @disnake.ui.button(label="Evet", emoji="✅")
    async def allow(self, button, interaction: disnake.MessageInteraction):
        self.selected = True
        self.interaction_resp = interaction
        self.stop()

    @disnake.ui.button(label="HAYIR", emoji="❌")
    async def deny(self, button, interaction: disnake.MessageInteraction):
        self.selected = False
        self.interaction_resp = interaction
        self.stop()

youtube_regex = r"https?://www\.youtube\.com/(?:channel/|@)[^/]+"
soundcloud_regex = r"^(?:https?:\/\/)?(?:www\.)?soundcloud\.com\/([a-zA-Z0-9_-]+)"

async def process_idle_embed(bot: BotCore, guild: disnake.Guild, guild_data: dict):

    try:
        bot.music.players[guild.id]
        return
    except KeyError:
        pass

    try:
        channel = bot.get_channel(int(guild_data["player_controller"]["channel"]))
    except:
        return

    try:
        message = await channel.fetch_message(int(guild_data["player_controller"]["message_id"]))
    except:
        message = None

    await send_idle_embed(message or channel, bot=bot, guild_data=guild_data)

class ViewMode:
    fav_manager = "0"
    guild_fav_manager = "1"
    integrations_manager = "2"

class FavModalImport(disnake.ui.Modal):

    def __init__(self, view):

        self.view = view

        if self.view.mode == ViewMode.fav_manager:
            super().__init__(
                title="Favorileri İçe Aktar",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Veri ekleyin (json formatında)",
                        custom_id="json_data",
                        min_length=20,
                        required=True
                    )
                ]
            )
            return

        if self.view.mode == ViewMode.guild_fav_manager:
            super().__init__(
                title="Çalma Listelerini Sunucuya Aktar",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Veri ekleyin (json formatında)",
                        custom_id="json_data",
                        min_length=20,
                        required=True
                    )
                ]
            )
            return

        if self.view.mode == ViewMode.integrations_manager:
            super().__init__(
                title="İçe aktarma entegrasyonu",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Veri ekleyin (json formatında)",
                        custom_id="json_data",
                        min_length=20,
                        required=True
                    )
                ]
            )
            return

        raise GenericError(f"Mevcut mod henüz uygulanmadı: {self.view.mode} | {type(self.view.mode)}")

    async def callback(self, inter: disnake.ModalInteraction, /) -> None:

        try:
            json_data = json.loads(inter.text_values["json_data"])
        except Exception as e:
            await inter.send("**Veriler ayrıştırılırken bir hata oluştu veya geçersiz/biçimlendirilmemiş veriler gönderildi "
                               f"json formatında.**\n\n`{repr(e)}`", ephemeral=True)
            return

        if self.view.mode == ViewMode.fav_manager:

            if retry_after := self.view.bot.get_cog("Music").fav_import_export_cd.get_bucket(inter).update_rate_limit():
                if retry_after < 1:
                    retry_after = 1
                await inter.send("**{}'ın içe aktarılmasını beklemelisiniz.**".format(
                    time_format(int(retry_after) * 1000, use_names=True)), ephemeral=True)
                return

            for name, url in json_data.items():

                if "> fav:" in name.lower():
                    continue

                if len(url) > (max_url_chars := self.view.bot.config["USER_FAV_MAX_URL_LENGTH"]):
                    await inter.send(
                        f"**{url} dosyanızdaki bir öğe izin verilen karakter sayısını aşıyor:{max_url_chars}**",
                        ephemeral=True)
                    return

                if not isinstance(url, str) or not URL_REG.match(url):
                    await inter.send(f"Dosyanız geçersiz bir bağlantı içeriyor: ```ldif\n{url}```", ephemeral=True)
                    return

            await inter.response.defer(ephemeral=True)

            self.view.data = await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users)

            for name in json_data.keys():
                if len(name) > (max_name_chars := self.view.bot.config["USER_FAV_MAX_NAME_LENGTH"]):
                    await inter.edit_original_message(
                        f"**Dosyanızdaki ({name}) bir öğe izin verilen karakter sayısını aşıyor:{max_name_chars}**")
                    return
                try:
                    del self.view.data["fav_links"][name.lower()]
                except KeyError:
                    continue

            if self.view.bot.config["MAX_USER_FAVS"] > 0 and not (await self.view.bot.is_owner(inter.author)):

                if (json_size := len(json_data)) > self.view.bot.config["MAX_USER_FAVS"]:
                    await inter.edit_original_message(f"Favori dosyanızdaki öğe sayısı şunu aşıyor: "
                                                      f"izin verilen maksimum miktar({self.view.bot.config['MAX_USER_FAVS']}).")
                    return

                if (json_size + (user_favs := len(self.view.data["fav_links"]))) > self.view.bot.config[
                    "MAX_USER_FAVS"]:
                    await inter.edit_original_message(
                        "Dosyanızdaki tüm yer işaretlerini eklemek için yeterli alanınız yok...\n"
                        f"Akım sınırı: {self.view.bot.config['MAX_USER_FAVS']}\n"
                        f"Kaydedilen favorilerin sayısı: {user_favs}\n"
                        f"Gerekenler: {(json_size + user_favs) - self.view.bot.config['MAX_USER_FAVS']}")
                    return

            self.view.data["fav_links"].update(json_data)

            await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

            await inter.edit_original_message(content="**Favoriler başarıyla içe aktarıldı!**")

            if (s := len(json_data)) > 1:
                self.view.log = f"{s} Favoriler başarıyla içe aktarıldı."
            else:
                name = next(iter(json_data))
                self.view.log = f"[`{name}`]({json_data[name]}) yer işareti başarıyla içe aktarıldı."


        elif self.view.mode == ViewMode.guild_fav_manager:

            if retry_after := self.view.bot.get_cog("Music").fav_import_export_cd.get_bucket(inter).update_rate_limit():
                if retry_after < 1:
                    retry_after = 1
                await inter.send("**İçe aktarmak için {} süre beklemelisiniz.**".format(
                    time_format(int(retry_after) * 1000, use_names=True)), ephemeral=True)
                return

            for name, data in json_data.items():

                if "> fav:" in name.lower():
                    continue

                if len(data['url']) > (max_url_chars := self.view.bot.config["USER_FAV_MAX_URL_LENGTH"]):
                    await inter.send(
                        f"**Dosyanızdaki bir öğe izin verilen karakter sayısını aşıyor:{max_url_chars}\nURL:** {data['url']}",
                        ephemeral=True)
                    return

                if len(data['description']) > 50:
                    await inter.send(
                        f"**Dosyanızdaki bir öğe izin verilen karakter sayısını aşıyor:{max_url_chars}\nAçıklama:** {data['description']}",
                        ephemeral=True)
                    return

                if not isinstance(data['url'], str) or not URL_REG.match(data['url']):
                    await inter.send(f"Dosyanız geçersiz bir bağlantı içeriyor: ```ldif\n{data['url']}```", ephemeral=True)
                    return

            await inter.response.defer(ephemeral=True)

            self.view.guild_data = await self.view.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if not self.view.guild_data["player_controller"]["channel"] or not self.view.bot.get_channel(
                    int(self.view.guild_data["player_controller"]["channel"])):
                await inter.edit_original_message("**Sunucuda yapılandırılmış oyuncu yok! /setup komutunu kullanın**")
                return

            for name in json_data.keys():
                if len(name) > (max_name_chars := 25):
                    await inter.edit_original_message(
                        f"**Dosyanızdaki ({name}) bir öğe izin verilen karakter sayısını aşıyor:{max_name_chars}**")
                    return
                try:
                    del self.view.guild_data["player_controller"]["fav_links"][name]
                except KeyError:
                    continue

            if (json_size := len(json_data)) > 25:
                await inter.edit_original_message(
                    f"Arşivdeki öğe sayısı izin verilen maksimum miktarı (25) aşıyor.")
                return

            if (json_size + (user_favs := len(self.view.guild_data["player_controller"]["fav_links"]))) > 25:
                await inter.edit_original_message(
                    "Sunucunun müzik listesi/çalma listesi, dosyanızdaki tüm öğeleri eklemek için yeterli alana sahip değil..\n"
                    f"Akım sınırı: 25\n"
                    f"Kaydedilen bağlantıların sayısı: {user_favs}\n"
                    f"Gerek: {(json_size + user_favs) - 25}")
                return

            self.view.guild_data["player_controller"]["fav_links"].update(json_data)

            await self.view.bot.update_data(inter.guild_id, self.view.guild_data, db_name=DBModel.guilds)

            guild = self.view.bot.get_guild(inter.guild_id)

            await inter.edit_original_message(content="**Sabit sunucu bağlantıları başarıyla içe aktarıldı!**")

            if (s := len(json_data)) > 1:
                self.view.log = f"{s} Bağlantılar başarıyla sunucunun favoriler listesine aktarıldı."
            else:
                name = next(iter(json_data))
                self.view.log = f"[`{name}`]({json_data[name]}) bağlantısı başarıyla sunucunun bağlantı listesine aktarıldı."

            await process_idle_embed(self.view.bot, guild, guild_data=self.view.guild_data)

        elif self.view.mode == ViewMode.integrations_manager:

            if retry_after := self.view.bot.get_cog("Music").fav_import_export_cd.get_bucket(inter).update_rate_limit():
                if retry_after < 1:
                    retry_after = 1
                await inter.send("**{}'ın içe aktarılmasını beklemelisiniz.**".format(
                    time_format(int(retry_after) * 1000, use_names=True)), ephemeral=True)
                return

            for name, url in json_data.items():

                if "> itg:" in name.lower():
                    continue

                if len(url) > (max_url_chars := 150):
                    await inter.edit_original_message(
                        f"**{url} dosyanızdaki bir öğe izin verilen karakter sayısını aşıyor:{max_url_chars}**")
                    return

                if not isinstance(url, str) or not URL_REG.match(url):
                    await inter.edit_original_message(f"Dosyanız geçersiz bir bağlantı içeriyor: ```ldif\n{url}```")
                    return

            await inter.response.defer(ephemeral=True)

            self.view.data = await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users)

            for name in json_data.keys():
                try:
                    del self.view.data["integration_links"][name.lower()[:90]]
                except KeyError:
                    continue

            if self.view.bot.config["MAX_USER_INTEGRATIONS"] > 0 and not (await self.view.bot.is_owner(inter.author)):

                if (json_size := len(json_data)) > self.view.bot.config["MAX_USER_INTEGRATIONS"]:
                    await inter.edit_original_message(f"Entegrasyon dosyanızdaki öğe sayısı şunu aşıyor: "
                                       f"izin verilen maksimum miktar ({self.view.bot.config['MAX_USER_INTEGRATIONS']}).")
                    return

                if (json_size + (user_integrations := len(self.view.data["integration_links"]))) > self.view.bot.config[
                    "MAX_USER_INTEGRATIONS"]:
                    await inter.edit_original_message(
                        "Dosyanızdaki tüm entegrasyonları eklemek için yeterli alanınız yok...\n"
                        f"Akım sınırı: {self.view.bot.config['MAX_USER_INTEGRATIONS']}\n"
                        f"Kaydedilen entegrasyonların sayısı: {user_integrations}\n"
                        f"Gerek: {(json_size + user_integrations) - self.view.bot.config['MAX_USER_INTEGRATIONS']}")
                    return

            self.view.data["integration_links"].update(json_data)

            await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

            await inter.edit_original_message(
                content="**Entegrasyonlar başarıyla içe aktarıldı!**"
            )

            if (s := len(json_data)) > 1:
                self.view.log = f"{s} entegrasyonlar başarıyla içe aktarıldı."
            else:
                name = next(iter(json_data))
                self.view.log = f"[`{name}`]({json_data[name]}) entegrasyonu başarıyla içe aktarıldı."

        else:
            raise GenericError(f"**Modo ainda não implementado: {self.view.mode} | {type(self.view.mode)}**")

        if not isinstance(self.view.ctx, CustomContext):
            await self.view.ctx.edit_original_message(embed=self.view.build_embed(), view=self.view)
        elif self.view.message:
            await self.view.message.edit(embed=self.view.build_embed(), view=self.view)


class FavModalAdd(disnake.ui.Modal):
    def __init__(self, name: Optional[str], url: Optional[str], view, **kwargs):

        self.view = view
        self.name = name

        if self.view.mode == ViewMode.fav_manager:
            super().__init__(
                title="Çalma listesi/favori Ekle/Düzenle",
                custom_id="user_fav_edit",
                timeout=180,
                components=[
                    disnake.ui.TextInput(
                        label="Playlist Favori/Favorileri:",
                        custom_id="user_fav_name",
                        min_length=2,
                        max_length=25,
                        value=name or None
                    ),
                    disnake.ui.TextInput(
                        label="Link/Url:",
                        custom_id="user_fav_url",
                        min_length=10,
                        max_length=200,
                        value=url or None
                    ),
                ]
            )
            return

        if self.view.mode == ViewMode.guild_fav_manager:
            super().__init__(
                title="Çalma listesi/favori Ekle/Düzenle",
                custom_id="guild_fav_edit",
                timeout=180,
                components=[
                    disnake.ui.TextInput(
                        label="Favori/çalma listesi adı:",
                        custom_id="guild_fav_name",
                        min_length=2,
                        max_length=25,
                        value=name or None
                    ),
                    disnake.ui.TextInput(
                        label="Açıklama:",
                        custom_id="guild_fav_description",
                        min_length=3,
                        max_length=50,
                        value=kwargs.get('description', None),
                        required=False
                    ),
                    disnake.ui.TextInput(
                        label="Link/Url:",
                        custom_id="guild_fav_url",
                        min_length=10,
                        max_length=250,
                        value=url or None
                    ),
                ]
            )
            return

        if self.view.mode == ViewMode.integrations_manager:
            super().__init__(
                title="Entegrasyon ekle",
                custom_id="user_integration_add",
                timeout=180,
                components=[
                    disnake.ui.TextInput(
                        label="Link/Url:",
                        custom_id="user_integration_url",
                        min_length=10,
                        max_length=200,
                        value=url or None
                    ),
                ]
            )
            return

        raise GenericError(f"**Henüz uygulanmayan mod: {self.view.mode} | {type(self.view.mode)}**")


    async def callback(self, inter: disnake.ModalInteraction):

        if self.view.mode == ViewMode.fav_manager:

            url = inter.text_values["user_fav_url"].strip()

            try:
                valid_url = URL_REG.findall(url)[0]
            except IndexError:
                await inter.send(
                    embed=disnake.Embed(
                        description=f"**Geçerli bağlantı bulunamadı:** {url}",
                        color=disnake.Color.red()
                    ), ephemeral=True
                )
                return

            await inter.response.defer(ephemeral=True)

            self.view.data = await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users)

            name = inter.text_values["user_fav_name"].strip()

            try:
                if name != self.name:
                    del self.view.data["fav_links"][self.name]
            except KeyError:
                if len(self.view.data["fav_links"]) >= self.view.bot.config["MAX_USER_FAVS"]:
                    await inter.edit_original_message(
                        "**Yeni favoriler eklemek için kullanılabilir alan yok (bazılarını kaldırıp tekrar deneyin).**")
                    return

            self.view.data["fav_links"][name] = valid_url

            await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

            try:
                me = (inter.guild or self.view.bot.get_guild(inter.guild_id)).me
            except AttributeError:
                me = None

            await inter.edit_original_message(
                embed=disnake.Embed(
                    description="**Bağlantı favorilerinize başarıyla kaydedildi/güncellendi!\n"
                                "Aşağıdaki durumlarda görünecektir:** ```\n"
                                "- /play komutunu kullanırken (arama otomatik tamamlamada seçim)\n"
                                "- Oyuncunun favori oynat düğmesine tıklayarak.\n"
                                "- Oynat (önekli) komutunu isim veya bağlantı olmadan kullanırken.```",
                    color=self.view.bot.get_color(me)
                )
            )

        elif self.view.mode == ViewMode.guild_fav_manager:
            url = inter.text_values["guild_fav_url"].strip()

            try:
                valid_url = URL_REG.findall(url)[0]
            except IndexError:
                await inter.send(
                    embed=disnake.Embed(
                        description=f"**Geçerli bağlantı bulunamadı:** {url}",
                        color=disnake.Color.red()
                    ), ephemeral=True
                )
                return

            await inter.response.defer(ephemeral=True)

            self.view.guild_data = await self.view.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if not self.view.guild_data["player_controller"]["channel"] or not self.view.bot.get_channel(
                    int(self.view.guild_data["player_controller"]["channel"])):
                await inter.edit_original_message("**Sunucuda yapılandırılmış oyuncu yok! /setup komutunu kullanın**")
                return

            name = inter.text_values["guild_fav_name"].strip()
            description = inter.text_values["guild_fav_description"].strip()

            if not self.view.guild_data["player_controller"]["channel"] or not self.view.bot.get_channel(
                    int(self.view.guild_data["player_controller"]["channel"])):
                await inter.edit_original_message("**Sunucuda yapılandırılmış oyuncu yok! /setup komutunu kullanın**")
                return

            try:
                if name != self.name:
                    del self.view.guild_data["player_controller"]["fav_links"][self.name]
            except KeyError:
                if len(self.view.guild_data["player_controller"]["fav_links"]) > 24:
                    await inter.edit_original_message("**Yeni favoriler eklemek için kullanılabilir alan yok (bazılarını kaldırıp tekrar deneyin).**")
                    return

            self.view.guild_data["player_controller"]["fav_links"][name] = {'url': valid_url, "description": description}

            await self.view.bot.update_data(inter.guild_id, self.view.guild_data, db_name=DBModel.guilds)

            guild = inter.guild or self.view.bot.get_guild(inter.guild_id)

            await inter.edit_original_message(
                embed=disnake.Embed(description="**Bağlantı oyuncunun sabit hatlarına başarıyla eklendi/güncellendi!\n"
                                                "Üyeler, kullanılmadığında doğrudan oynatıcı-kontrol cihazında kullanabilir.**",
                                    color=self.view.bot.get_color(guild.me)), view=None)

            await process_idle_embed(self.view.bot, guild, guild_data=self.view.guild_data)

        elif self.view.mode == ViewMode.integrations_manager:

            try:
                await inter.response.defer(ephemeral=True, with_message=True)
            except:
                pass

            self.view.data = await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users)

            if len(self.view.data["integration_links"]) >= self.view.bot.config["MAX_USER_FAVS"]:
                await inter.edit_original_message(
                    "**Yeni entegrasyonlar eklemek için kullanılabilir alan yok (herhangi birini kaldırıp tekrar deneyin).**")
                return

            url = inter.text_values["user_integration_url"].strip()

            try:
                url = URL_REG.findall(url)[0]
            except IndexError:
                await inter.edit_original_message(
                    embed=disnake.Embed(
                        description=f"**Geçerli bağlantı bulunamadı:** {url}",
                        color=disnake.Color.red()
                    )
                )
                return

            if (matches := spotify_regex_w_user.match(url)):

                if not self.view.bot.spotify:
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description="**Spotify desteği şu anda kullanılamıyor...**",
                            color=disnake.Color.red()
                        )
                    )
                    return

                url_type, user_id = matches.groups()

                if url_type != "user":
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description=f"**Bir Spotify kullanıcı profili bağlantısı kullanmalısınız.** {url}",
                            color=disnake.Color.red()
                        )
                    )
                    return

                try:
                    result = await self.view.bot.spotify.get_user_info(user_id)
                except Exception as e:
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description="**Spotify'dan bilgi alınırken bir hata oluştu:** ```py\n"
                                        f"{repr(e)}```",
                            color=self.view.bot.get_color()
                        )
                    )
                    traceback.print_exc()
                    return

                if not result:
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description="**Sağlanan bağlantının kullanıcısının herkese açık oynatma listeleri yok...**",
                            color=self.view.bot.get_color()
                        )
                    )
                    return

                data = {"title": f"[SP]: {result['display_name'][:90]}", "url": result["external_urls"]["spotify"]}

            elif (matches:=deezer_regex.match(url)):

                url_type, url_id = matches.groups()[-2:]

                if url_type != "profile":
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description=f"**Deezer kullanıcı profilindeki bir bağlantıyı kullanmalısınız.** {url}",
                            color=disnake.Color.red()
                        )
                    )
                    return

                try:
                    result = await self.view.bot.deezer.get_user_info(int(url_id))
                except Exception as e:
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description="**Spotify'dan bilgi alınırken bir hata oluştu:** ```py\n"
                                        f"{repr(e)}```",
                            color=self.view.bot.get_color()
                        )
                    )
                    traceback.print_exc()
                    return

                data = {"title": f"[DZ]: {result['name'][:90]}", "url": result['link']}

            else:

                if not self.view.bot.config["USE_YTDL"]:
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description="**Bu tür bağlantı şu anda desteklenmiyor...**",
                            color=self.view.bot.get_color()
                        )
                    )
                    return

                match = re.search(youtube_regex, url)

                if match:
                    base_url = f"{match.group(0)}/playlists"
                    source = "[YT]:"
                else:
                    match = re.search(soundcloud_regex, url)
                    if match:
                        group = match.group(1)
                        base_url = f"https://soundcloud.com/{group}/sets"
                    else:
                        await inter.edit_original_message(
                            embed=disnake.Embed(
                                description=f"**Sağlanan bağlantı desteklenmiyor:** {url}",
                                color=disnake.Color.red()
                            )
                        )
                        return

                    source = "[SC]:"

                loop = self.view.bot.loop or asyncio.get_event_loop()

                try:
                    info = await loop.run_in_executor(None, lambda: self.view.bot.pool.ytdl.extract_info(base_url, download=False))
                except Exception as e:
                    traceback.print_exc()
                    await inter.edit_original_message(f"**URL'den bilgi alınırken bir hata oluştu:** ```py\n{repr(e)}```")
                    return

                if not info:

                    msg = f"**Sağlanan bağlantının kullanıcısı/kanalı mevcut değil:**\n{url}"

                    if source == "[YT]:":
                        msg += f"\n\n`Not: Bağlantının @ karakterine sahip bir kullanıcı içerip içermediğini kontrol edin; ör. @ytchannel.`"

                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description=msg,
                            color=disnake.Color.red()
                        )
                    )
                    return

                if not info['entries']:
                    await inter.edit_original_message(
                        embed=disnake.Embed(
                            description=f"**Sağlanan bağlantıdaki kullanıcı/kanalın herkese açık oynatma listeleri yok...**",
                            color=disnake.Color.red()
                        )
                    )
                    return

                data = {"title": f"{source} {info['title']}", "url": info["original_url"]}

            title = fix_characters(data['title'], 80)

            self.view.data["integration_links"][title] = data['url']

            await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

            try:
                me = (inter.guild or self.view.bot.get_guild(inter.guild_id)).me
            except AttributeError:
                me = None

            await inter.edit_original_message(
                embed=disnake.Embed(
                    description=f"**Entegrasyon başarıyla eklendi/düzenlendi:** [`{title}`]({data['url']})\n"
                                "**Aşağıdaki durumlarda ortaya çıkacak:** ```\n"
                                "- /play komutunu kullanırken (arama otomatik tamamlamada entegrasyonu seçme))\n"
                                "- Oyuncunun favori oynat düğmesine tıklayarak.\n"
                                "- Oynat (önekli) komutunu isim veya bağlantı olmadan kullanırken.```",
                    color=self.view.bot.get_color(me)
                ), view=None
            )

            self.view.log = f"[`{data['title']}`]({data['url']}) entegrasyonlarınıza eklendi."

        if not isinstance(self.view.ctx, CustomContext):
            await self.view.ctx.edit_original_message(embed=self.view.build_embed(), view=self.view)
        elif self.view.message:
            await self.view.message.edit(embed=self.view.build_embed(), view=self.view)

class FavMenuView(disnake.ui.View):

    def __init__(self, bot: BotCore, ctx: Union[disnake.AppCmdInter, CustomContext], data: dict, log: str = "",
                 prefix="", mode: str = ViewMode.fav_manager, is_owner=False):
        super().__init__(timeout=180)
        self.mode = mode
        self.bot = bot
        self.ctx = ctx
        self.guild = ctx.guild
        self.current = None
        self.data = data
        self.guild_data = {}
        self.message = None
        self.log = log
        self.prefix = prefix
        self.components_updater_task = bot.loop.create_task(self.auto_update())
        self.is_owner = is_owner
        self.light_mode = False

        if not self.guild:
            for b in self.bot.pool.get_guild_bots(ctx.guild_id):
                guild = b.get_guild(ctx.guild_id)
                if guild:
                    self.guild = guild
                    break

            if not self.guild:
                self.light_mode = True

    def update_components(self):

        self.clear_items()

        if not self.light_mode:

            mode_select = disnake.ui.Select(
                options=[
                    disnake.SelectOption(label="Favoriler Yöneticisi", value=f"fav_view_mode_{ViewMode.fav_manager}", emoji="⭐",
                                         default=self.mode == ViewMode.fav_manager)
                ], min_values=1, max_values=1
            )

            mode_select.append_option(
                disnake.SelectOption(label="Entegrasyon Yöneticisi", value=f"fav_view_mode_{ViewMode.integrations_manager}", emoji="💠",
                                     default=self.mode == ViewMode.integrations_manager)
            )

            if self.guild and (self.ctx.author.guild_permissions.manage_guild or self.is_owner):
                mode_select.options.insert(1, disnake.SelectOption(label="Sunucu Oynatma Listesi Yöneticisi",
                                                                   value=f"fav_view_mode_{ViewMode.guild_fav_manager}", emoji="📌",
                                                                   default=self.mode == ViewMode.guild_fav_manager))

            if len(mode_select.options) < 2:
                mode_select.disabled = True

            mode_select.callback = self.mode_callback
            self.add_item(mode_select)

        if self.mode == ViewMode.fav_manager:

            if self.data["fav_links"]:
                opts = []
                for k, v in list(self.data["fav_links"].items())[:25]: # TODO: Lidar depois com os dados existentes que excedem a quantidade permitida
                    emoji, platform = music_source_emoji_url(v)
                    opts.append(disnake.SelectOption(label=k, emoji=emoji, description=platform))
                fav_select = disnake.ui.Select(options=opts, min_values=1, max_values=1)
                fav_select.options[0].default = True
                self.current = fav_select.options[0].label
                fav_select.callback = self.select_callback
                self.add_item(fav_select)

        elif self.mode == ViewMode.guild_fav_manager and self.guild:

            bots_in_guild = []

            for b in sorted(self.bot.pool.get_guild_bots(self.guild.id), key=lambda b: b.identifier):
                if b.bot_ready and b.user in self.guild.members:
                    bots_in_guild.append(disnake.SelectOption(emoji="🎶",
                                                              label=f"Bot: {b.user.display_name}"[:25],
                                                              value=f"bot_select_{b.user.id}",
                                                              description=f"ID: {b.user.id}", default=b == self.bot))

            if bots_in_guild:
                bot_select = disnake.ui.Select(options=bots_in_guild, min_values=1, max_values=1)
                bot_select.callback = self.bot_select
                self.add_item(bot_select)

            if self.guild_data["player_controller"]["fav_links"]:
                fav_select = disnake.ui.Select(options=[
                    disnake.SelectOption(label=k, emoji=music_source_emoji_url(v['url'])[0],
                                         description=v.get("description")) for k, v in
                    list(self.guild_data["player_controller"]["fav_links"].items())[:25] # TODO: Lidar depois com os dados existentes que excedem a quantidade permitida
                ], min_values=1, max_values=1)
                fav_select.options[0].default = True
                self.current = fav_select.options[0].label
                fav_select.callback = self.select_callback
                self.add_item(fav_select)

        elif self.mode == ViewMode.integrations_manager:

            if self.data["integration_links"]:
                opts = []
                for k, v in list(self.data["integration_links"].items())[:25]: # TODO: Lidar depois com os dados existentes que excedem a quantidade permitida
                    emoji, platform = music_source_emoji_url(v)
                    opts.append(disnake.SelectOption(label=k[5:], emoji=emoji, description=platform, value=k))
                integration_select = disnake.ui.Select(options=opts, min_values=1, max_values=1)
                integration_select.options[0].default = True
                self.current = integration_select.options[0].label
                integration_select.callback = self.select_callback
                self.add_item(integration_select)

        add_button = disnake.ui.Button(label="Eklemek", emoji="<:add_music:588172015760965654>")
        add_button.callback = self.add_callback
        self.add_item(add_button)

        if self.mode == ViewMode.fav_manager:
            edit_button = disnake.ui.Button(label="Düzenlemek için", emoji="✍️", disabled=not self.data["fav_links"])
            edit_button.callback = self.edit_callback
            self.add_item(edit_button)

            remove_button = disnake.ui.Button(label="Kaldır", emoji="♻️", disabled=not self.data["fav_links"])
            remove_button.callback = self.remove_callback
            self.add_item(remove_button)

            clear_button = disnake.ui.Button(label="Favorileri temizle", emoji="🚮", disabled=not self.data["fav_links"])
            clear_button.callback = self.clear_callback
            self.add_item(clear_button)

            export_button = disnake.ui.Button(label="Dışa Aktar", emoji="📤", disabled=not self.data["fav_links"])
            export_button.callback = self.export_callback
            self.add_item(export_button)

        elif self.mode == ViewMode.guild_fav_manager:
            edit_button = disnake.ui.Button(label="Düzenlemek için", emoji="✍️", disabled=not self.guild_data["player_controller"]["fav_links"])
            edit_button.callback = self.edit_callback
            self.add_item(edit_button)

            remove_button = disnake.ui.Button(label="Kaldır", emoji="♻️", disabled=not self.guild_data["player_controller"]["fav_links"])
            remove_button.callback = self.remove_callback
            self.add_item(remove_button)

            clear_button = disnake.ui.Button(label="Favorileri temizle", emoji="🚮", disabled=not self.guild_data["player_controller"]["fav_links"])
            clear_button.callback = self.clear_callback
            self.add_item(clear_button)

            export_button = disnake.ui.Button(label="Dışa Aktar", emoji="📤", disabled=not self.guild_data["player_controller"]["fav_links"])
            export_button.callback = self.export_callback
            self.add_item(export_button)

        elif self.mode == ViewMode.integrations_manager:
            remove_button = disnake.ui.Button(label="Kaldır", emoji="♻️", disabled=not self.data["integration_links"])
            remove_button.callback = self.remove_callback
            self.add_item(remove_button)

            clear_button = disnake.ui.Button(label="Entegrasyonları Temizle", emoji="🚮", disabled=not self.data["integration_links"])
            clear_button.callback = self.clear_callback
            self.add_item(clear_button)

            export_button = disnake.ui.Button(label="Dışa Aktar", emoji="📤", disabled=not self.data["integration_links"])
            export_button.callback = self.export_callback
            self.add_item(export_button)

        import_button = disnake.ui.Button(label="İçe Aktar", emoji="📥")
        import_button.callback = self.import_callback
        self.add_item(import_button)

        if self.mode == ViewMode.fav_manager:
            if self.data["fav_links"] and not self.light_mode:
                play_button = disnake.ui.Button(label="Seçilen favoriyi çal", emoji="▶", custom_id="favmanager_play_button")
                play_button.callback = self.play_callback
                self.add_item(play_button)

        elif self.mode == ViewMode.integrations_manager:
            if self.data["integration_links"]:
                play_button = disnake.ui.Button(label="Seçilen entegrasyondan bir oynatma listesi oynatın", emoji="▶", custom_id="favmanager_play_button")
                play_button.callback = self.play_callback
                self.add_item(play_button)

        cancel_button = disnake.ui.Button(label="Fechar", emoji="❌")
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)

    async def auto_update(self):

        while True:

            if self.mode != ViewMode.fav_manager:
                return

            user, data, url = await self.bot.wait_for("fav_add", check=lambda user, data, url: user.id == self.ctx.author.id)

            self.log = f"{url} Favorilerinize eklendi."

            if not isinstance(self.ctx, CustomContext):
                await self.ctx.edit_original_message(content=self.build_txt(), view=self)
            elif self.message:
                await self.message.edit(content=self.build_txt(), view=self)

    async def on_timeout(self):

        try:
            self.components_updater_task.cancel()
        except:
            pass

        try:
            for i in self.children[1].options:
                i.default = self.current == i.value
        except:
            pass

        for c in self.children:
            c.disabled = True

        if isinstance(self.ctx, CustomContext):
            try:
                await self.message.edit(view=self)
            except:
                pass

        else:
            try:
                await self.ctx.edit_original_message(view=self)
            except:
                pass

        self.stop()

    def build_txt(self):

        supported_platforms = []

        if self.mode == ViewMode.integrations_manager:

            if self.bot.config["USE_YTDL"]:
                supported_platforms.extend(["[`Youtube`](<https://www.youtube.com/>)", "[`Soundcloud`](<https://soundcloud.com/>)"])

            if self.bot.spotify:
                supported_platforms.append("[`Spotify`](<https://open.spotify.com/>)")

            supported_platforms.append("[`Deezer`](<https://www.deezer.com/>)")

        self.update_components()

        try:
            cmd = f"</play:" + str(self.bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
        except AttributeError:
            cmd = "/play"

        if self.mode == ViewMode.fav_manager:

            txt = "### Favoriler yöneticisi.\n"

            if not self.data["fav_links"]:
                txt += "Favorileriniz yok (aşağıdaki ekle düğmesini tıklayın).\n"

            else:
                def format_fav(index, data):
                    name, url = data
                    e = get_source_emoji_cfg(self.bot, url)
                    if e:
                        return f"` {index:02} ` {e} [`{name}`](<{url}>)"
                    return f"` {index:02} ` [`{name}`](<{url}>)"

                txt += "\n".join(
                    f"> {format_fav(n+1, d)}" for n, d in enumerate(islice(self.data["fav_links"].items(), 25))
                )

            if not self.light_mode:
                txt += "\n\n**Bunları nasıl kullanıyorsunuz?**\n" \
                       f"* {cmd} komutunu kullanma (arama otomatik tamamlamasında favoriyi seçme)\n" \
                        "* Favori oynat düğmesine tıklamak/seçmek/oyuncu entegrasyonu.\n" \
                        f"* Komutu kullanma {self.prefix}{self.bot.get_cog('Music').play_legacy.name} bir şarkının/videonun adını veya bağlantısını eklemeden.\n" \
                        "* Aşağıdaki favori oynat düğmesini kullanma.\n"

        elif self.mode == ViewMode.guild_fav_manager:

            txt = "### Sunucu Sık Kullanılanları Yöneticisi.\n"

            if not self.guild_data["player_controller"]["fav_links"]:
                txt += f"Botta eklenmiş bağlantı yok {self.bot.user.mention} (Aşağıdaki ekle butonuna tıklayın).\n"

            else:
                def format_gfav(index, data):
                    name, data = data
                    e = get_source_emoji_cfg(self.bot, data['url'])
                    if e:
                        return f"` {index:02} ` {e} [`{name}`](<{data['url']}>)"
                    return f"` {index:02} ` [`{name}`](<{data['url']}>)"

                txt += f"**Bottaki mevcut bağlantılar {self.bot.user.mention}:**\n" + "\n".join(
                    f"> {format_gfav(n+1, d)}" for n, d in enumerate(islice(self.guild_data["player_controller"]["fav_links"].items(), 25))
                )

                txt += "\n\n**Bunları nasıl kullanıyorsunuz?**\n" \
                        f"* Bekleme modunda oynatıcı seçim menüsünün kullanılması.\n" \
                       f"\n`Bot seçildi:` {self.bot.user.mention}"

        elif self.mode == ViewMode.integrations_manager:

            txt = "### Herkese açık oynatma listelerine sahip kanal/profil entegrasyon yöneticisi.\n"

            if not self.data["integration_links"]:
                txt += "**Şu anda entegrasyonunuz yok (aşağıdaki ekle düğmesini tıklayın).**\n"

            else:
                def format_itg(bot, index, data):
                    name, url = data
                    e = get_source_emoji_cfg(bot, url)
                    if e:
                        return f"` {index:02} ` {e} [`{name[5:]}`](<{url}>)"
                    return f"` {index:02} ` [`{name}`](<{url}>)"

                txt += f"### Suas integrações atuais:\n" + "\n".join(
                    f"> {format_itg(self.bot, n+1, d)}" for n, d in enumerate(islice(self.data["integration_links"].items(), 25)))

                if not self.light_mode:
                    txt += "\n\n**Bunları nasıl kullanıyorsunuz?**\n" \
                           f"* {cmd} komutunu kullanma (arama otomatik tamamlamasında favoriyi seçme)\n" \
                           "* Favori oynat düğmesine tıklamak/seçmek/oyuncu entegrasyonu.\n" \
                           f"* Komutu kullanma {self.prefix}{self.bot.get_cog('Music').play_legacy.name} bir şarkının/videonun adını veya bağlantısını eklemeden.\n" \
                           "* Aşağıdaki favori oynat düğmesini kullanma.\n"

        else:
            raise GenericError(f"**Modo não implementado:** {self.mode} | {type(self.mode)}")

        if self.log:
            txt += f"\n**Son etkileşim:**\n{self.log}\n"

        if self.mode == ViewMode.integrations_manager:
            txt += f"\n**Desteklenen profil/kanal bağlantıları:**\n{', '.join(supported_platforms)}"

        return txt

    async def add_callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(FavModalAdd(name=None, url=None, view=self))

    async def edit_callback(self, inter: disnake.MessageInteraction):

        if not self.current:
            await inter.send("Bir öğe seçmelisiniz!", ephemeral=True)
            return

        if self.mode == ViewMode.fav_manager:
            try:
                await inter.response.send_modal(
                    FavModalAdd(name=self.current, url=self.data["fav_links"][self.current], view=self)
                )
            except KeyError:
                await inter.send(f"**Bu isimde favori yok:** {self.current}", ephemeral=True)

        elif self.mode == ViewMode.guild_fav_manager:
            guild = self.bot.get_guild(inter.guild_id) or inter.guild

            if not guild:
                await inter.send("Bu eylemi sunucunun dışında gerçekleştiremezsiniz.", ephemeral=True)
                return
            try:
                await inter.response.send_modal(
                    FavModalAdd(
                        bot=self.bot, name=self.current,
                        url=self.data["player_controller"]["fav_links"][self.current]["url"],
                        description=self.data["player_controller"]["fav_links"][self.current]["description"],
                        view=self
                    )
                )
            except KeyError:
                await inter.send(f"**İsmi olan bir favori yok:** {self.current}", ephemeral=True)

    async def remove_callback(self, inter: disnake.MessageInteraction):

        if not self.current:
            await inter.send("Bir öğe seçmelisiniz!", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)

        guild = None

        if self.mode == ViewMode.guild_fav_manager:

            guild = self.bot.get_guild(inter.guild_id)

            if not guild:
                await inter.send("Bu eylemi sunucunun dışında gerçekleştiremezsiniz.", ephemeral=True)
                return

            if not self.guild_data:
                self.guild_data = await self.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        else:
            self.data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        if self.mode == ViewMode.fav_manager:
            try:
                url = f'[`{self.current}`]({self.data["fav_links"][self.current]})'
                del self.data["fav_links"][self.current]
            except:
                await inter.edit_original_message(f"**Listede şu isimde favori yok:** {self.current}")
                return

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = f"Favori {url} başarıyla kaldırıldı!"

        elif self.mode == ViewMode.guild_fav_manager:
            try:
                url = f'[`{self.current}`]({self.guild_data["player_controller"]["fav_links"][self.current]})'
                del self.guild_data["player_controller"]["fav_links"][self.current]
            except KeyError:
                try:
                    await process_idle_embed(self.bot, guild, guild_data=self.guild_data)
                except Exception:
                    traceback.print_exc()

                await inter.edit_original_message(
                    embed=disnake.Embed(
                        description=f"**Şu adda liste bağlantısı yok:** {self.current}",
                        color=self.bot.get_color(guild.me)),
                    view=None
                )
                return

            await self.bot.update_data(inter.guild_id, self.guild_data, db_name=DBModel.guilds)

            self.log = f"Bağlantı {url}, sunucu favorileri listesinden başarıyla kaldırıldı!"

        elif self.mode == ViewMode.integrations_manager:
            try:
                url = f'[`{self.current}`]({self.data["integration_links"][self.current]})'
                del self.data["integration_links"][self.current]
            except:
                await inter.send(f"**Listede isimle entegrasyon yok:** {self.current}", ephemeral=True)
                return

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = f"Entegrasyon {url} başarıyla kaldırıldı!"

        await inter.edit_original_message(content=self.build_txt(), view=self)

    async def bot_select(self, inter: disnake.MessageInteraction):

        value = int(inter.values[0][11:])

        for b in self.bot.pool.get_guild_bots(inter.guild_id):
            try:
                if b.user.id == value:
                    self.bot = b
            except AttributeError:
                continue

        self.guild_data = await self.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        await inter.response.edit_message(content=self.build_txt(), view=self)

    async def clear_callback(self, inter: disnake.MessageInteraction):

        guild = None

        if self.mode == ViewMode.guild_fav_manager:

            guild = self.bot.get_guild(inter.guild_id) or inter.guild

            if not guild:
                await inter.send("Bu eylemi sunucunun dışında gerçekleştiremezsiniz.", ephemeral=True)
                return

            await inter.response.defer(ephemeral=True)

            if not self.guild_data:
                self.guild_data = await self.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        else:

            await inter.response.defer(ephemeral=True)

            self.data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        if self.mode == ViewMode.fav_manager:
            if not self.data["fav_links"]:
                await inter.send("**Favori bağlantınız yok!**", ephemeral=True)
                return

            fp = BytesIO(bytes(json.dumps(self.data["fav_links"], indent=4), 'utf-8'))

            self.data["fav_links"].clear()

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = "Favori listeniz başarıyla temizlendi!"

            await inter.send("### Yer işaretleriniz başarıyla silindi!\n"
                             "`Bir yedekleme dosyası oluşturuldu ve bu silme işlemini geri almak istiyorsanız, yedekleme dosyasını kopyalayın. "
                             "dosya içeriğini seçin ve \"içe aktar\" düğmesine tıklayın ve içeriği belirtilen alana yapıştırın.`",
                             ephemeral=True, file=disnake.File(fp, filename="favs.json"))

        elif self.mode == ViewMode.guild_fav_manager:

            if not self.guild_data["player_controller"]["fav_links"]:
                await inter.send("**Sunucuda favori bağlantı yok.**", ephemeral=True)
                return

            fp = BytesIO(bytes(json.dumps(self.guild_data["player_controller"]["fav_links"], indent=4), 'utf-8'))

            self.guild_data["player_controller"]["fav_links"].clear()

            await self.bot.update_data(inter.guild_id, self.guild_data, db_name=DBModel.guilds)

            try:
                await process_idle_embed(self.bot, guild, guild_data=self.guild_data)
            except:
                traceback.print_exc()

            self.log = "Sunucu favorileri listesi başarıyla temizlendi!"

            await inter.send("### Sunucu yer işareti bağlantıları başarıyla silindi!\n"
                             "`bir yedekleme dosyası oluşturuldu ve bu silme işlemini geri almak istiyorsanız, yedekleme dosyasını kopyalayın. "
                             "dosya içeriğini seçin ve \"içe aktar\" düğmesine tıklayın ve içeriği belirtilen alana yapıştırın.`",
                             ephemeral=True, file=disnake.File(fp, filename="guild_favs.json"))

        elif self.mode == ViewMode.integrations_manager:

            if not self.data["integration_links"]:
                await inter.response.edit_message(content="**Kaydedilmiş entegrasyonunuz yok!**", view=None)
                return

            fp = BytesIO(bytes(json.dumps(self.data["integration_links"], indent=4), 'utf-8'))

            self.data["integration_links"].clear()

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = "Entegrasyon listeniz başarıyla temizlendi!"

            await inter.send("### Entegrasyonlarınız başarıyla silindi!\n"
                             "`bir yedekleme dosyası oluşturuldu ve bu silme işlemini geri almak istiyorsanız, yedekleme dosyasını kopyalayın. "
                             "dosya içeriğini seçin ve \"içe aktar\" düğmesine tıklayın ve içeriği belirtilen alana yapıştırın.`",
                             ephemeral=True, file=disnake.File(fp, filename="integrations.json"))

        self.current = None

        if not isinstance(self.ctx, CustomContext):
            await self.ctx.edit_original_message(content=self.build_txt(), view=self)
        elif self.message:
            await self.message.edit(content=self.build_txt(), view=self)

    async def import_callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(FavModalImport(view=self))

    async def play_callback(self, inter: disnake.MessageInteraction):
        await check_pool_bots(inter, check_player=False)
        await self.bot.get_cog("Music").player_controller(inter, PlayerControls.enqueue_fav, query=f"> itg: {self.current}" if self.mode == ViewMode.integrations_manager else f"> fav: {self.current}")

    async def export_callback(self, inter: disnake.MessageInteraction):
        cog = self.bot.get_cog("Music")

        if retry_after := cog.fav_import_export_cd.get_bucket(inter).update_rate_limit():
            if retry_after < 1:
                retry_after = 1
            await inter.send("**Dışa aktarma için {}'ı beklemeniz gerekir.**".format(
                time_format(int(retry_after) * 1000, use_names=True)), ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)

        try:
            cmd = f"</{cog.fav_manager.name}:" + str(
                self.bot.get_global_command_named(cog.fav_manager.name,
                                                                      cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
        except AttributeError:
            cmd = "/play"

        if self.mode == ViewMode.fav_manager:
            if not self.data["fav_links"]:
                await inter.send(f"**Favori bağlantılarınız yok..\n"
                                 f"Komutu kullanarak ekleyebilirsiniz: {cmd}**", ephemeral=True)
                return

            fp = BytesIO(bytes(json.dumps(self.data["fav_links"], indent=4), 'utf-8'))

            await inter.send(embed=disnake.Embed(
                description=f"Favorileriniz burada.\Komutu kullanarak içe aktarabilirsiniz: {cmd}",
                color=self.bot.get_color()), file=disnake.File(fp=fp, filename="favoritos.json"), ephemeral=True)

        elif self.mode == ViewMode.guild_fav_manager:
            if not self.guild_data["player_controller"]["fav_links"]:
                await inter.edit_original_message(content=f"**Sunucuya sabitlenmiş şarkı/çalma listesi yok.\n"
                                                          f"Komutu kullanarak ekleyebilirsiniz: {cmd}**")

            fp = BytesIO(bytes(json.dumps(self.guild_data["player_controller"]["fav_links"], indent=4), 'utf-8'))

            guild = self.bot.get_guild(inter.guild_id) or inter.guild

            embed = disnake.Embed(
                description=f"**Sunucunun sabit şarkı/çalma listesi bağlantı verileri buradadır.\n"
                            f"Şu komutu kullanarak içe aktarabilirsiniz:** {cmd}",
                color=self.bot.get_color(guild.me))

            await inter.send(embed=embed, file=disnake.File(fp=fp, filename="guild_favs.json"), ephemeral=True)

        elif self.mode == ViewMode.integrations_manager:

            if not self.data["integration_links"]:
                await inter.edit_original_message(f"**Eklenen entegrasyonunuz yok...\n"
                                                  f"Komutu kullanarak ekleyebilirsiniz: {cmd}**")
                return

            fp = BytesIO(bytes(json.dumps(self.data["integration_links"], indent=4), 'utf-8'))

            await inter.send(embed=disnake.Embed(
                description=f"Entegrasyonlarınız burada.\nKomutu kullanarak içe aktarabilirsiniz: {cmd}",
                color=self.bot.get_color()), file=disnake.File(fp=fp, filename="integrations.json"), ephemeral=True)

    async def cancel_callback(self, inter: disnake.MessageInteraction):

        try:
            self.components_updater_task.cancel()
        except:
            pass

        await inter.response.edit_message(
            embed=disnake.Embed(
                description="**Kapalı yönetici.**",
                color=self.bot.get_color(),
            ), view=None
        )
        self.stop()

    async def mode_callback(self, inter: disnake.MessageInteraction):
        self.mode = inter.values[0][14:]

        try:
            self.components_updater_task.cancel()
        except:
            pass

        if self.mode == ViewMode.fav_manager:
            self.components_updater_task = self.bot.loop.create_task(self.auto_update())

        elif self.mode == ViewMode.guild_fav_manager:
            if not self.guild_data:
                await inter.response.defer()
                self.guild_data = await self.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        if inter.response.is_done():
            await inter.edit_original_message(content=self.build_txt(), view=self)
        else:
            await inter.response.edit_message(content=self.build_txt(), view=self)

    async def select_callback(self, inter: disnake.MessageInteraction):
        self.current = inter.values[0]
        await inter.response.defer()

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:

        if inter.author.id == self.ctx.author.id:
            return True

        await inter.send(f"Bu mesajla yalnızca {self.ctx.author.mention} üyesi etkileşim kurabilir.", ephemeral=True)



base_skin = {
    "queue_max_entries": 7,
    "queue_format": "`{track.number}) [{track.duration}]` [`{track.title_42}`]({track.url})",
    "embeds": [
        {
            "title": "Sonraki şarkılar:",
            "description": "{queue_format}",
            "color": "{guild.color}"
        },
        {
            "description": "**Şimdi çalıyor:\n[{track.title}]({track.url})**\n\n**Süre:** `{track.duration}`\n**Talep eden:** {requester.mention}\n**Yükleyici**: `{track.author}`\n**Kaynak oynatma listesi:** [`{playlist.name}`]({playlist.url})\n\n{player.log.emoji} **Son eylem:** {player.log.text}",
            "image": {
              "url": "{track.thumb}"
            },
            "color": "{guild.color}",
            "footer": {
               "text": "Listedeki şarkılar: {player.queue.size}"
            }
        }
    ]
}


class SkinSettingsButton(disnake.ui.View):

    def __init__(self, user: disnake.Member, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.mode = "custom_skins_static"
        self.inter = None
        self.controller_enabled = True
        self.update_components()

    def update_components(self):

        self.clear_items()

        select_mode = disnake.ui.Select(
            min_values=1, max_values=1, options=[
                disnake.SelectOption(label="Modo Normal", description="Görünümü normal oynatıcı moduna uygula",
                                     value="custom_skins", default=self.mode == "custom_skins"),
                disnake.SelectOption(label="Song-Request", description="Uygulama dış görünümü modo şarkı isteği yok oyuncuya",
                                     value="custom_skins_static", default=self.mode == "custom_skins_static"),
            ]
        )
        select_mode.callback = self.player_mode
        self.add_item(select_mode)

        if self.mode == "custom_skins":
            controller_btn = disnake.ui.Button(emoji="💠",
                label="Oyuncu Kontrol Cihazını Etkinleştir" if not self.controller_enabled else "Oynatıcı Denetleyicisini Devre Dışı Bırak"
            )
            controller_btn.callback = self.controller_buttons
            self.add_item(controller_btn)

        save_btn = disnake.ui.Button(label="Kaydet", emoji="💾")
        save_btn.callback = self.save
        self.add_item(save_btn)

    async def controller_buttons(self, inter: disnake.MessageInteraction):
        self.controller_enabled = not self.controller_enabled
        self.update_components()
        await inter.response.edit_message(view=self)

    async def player_mode(self, inter: disnake.MessageInteraction):
        self.mode = inter.values[0]
        self.update_components()
        await inter.response.edit_message(view=self)

    async def save(self, inter: disnake.ModalInteraction):
        self.inter = inter
        self.stop()

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:

        if inter.user.id != self.user.id:
            await inter.send(f"Bu mesaj butonlarını yalnızca {self.user.mention} üyesi kullanabilir.", ephemeral=True)
            return False

        return True


class ViewModal(disnake.ui.Modal):

    def __init__(self, view: Union[SkinEditorMenu, QueueInteraction], title: str, components: List[disnake.TextInput], custom_id: str):
        self.view = view
        super().__init__(title=title, components=components, custom_id=custom_id)
    async def callback(self, inter: disnake.ModalInteraction, /) -> None:
        await self.view.modal_handler(inter)

class SetStageTitle(disnake.ui.View):

    placeholders = (
        '{track.title}', '{track.timestamp}', '{track.emoji}', '{track.author}', '{track.duration}',
        '{track.source}', '{track.playlist}',
        '{requester.name}', '{requester.id}'
    )

    placeholder_text = "```ansi\n[34;1m{track.title}[0m -> Şarkının adı\n" \
               "[34;1m{track.author}[0m -> Şarkının Sanatçısının/Yükleyicisinin/Yazarının adı.\n" \
               "[34;1m{track.duration}[0m -> Şarkı süresi.\n" \
               "[34;1m{track.timestamp}[0m -> Geri sayımda şarkı süresi (yalnızca ses kanalı).\n" \
               "[34;1m{track.source}[0m -> Müziğin kökeni/kaynağı (Youtube/Spotify/Soundcloud vb.)\n" \
               "[34;1m{track.emoji}[0m -> Müzik Kaynağı Emoji (Yalnızca Ses Kanalı).\n" \
               "[34;1m{track.playlist}[0m -> Şarkının kaynak çalma listesinin adı (varsa)\n" \
               "[34;1m{requester.name}[0m -> Şarkıyı talep eden üyenin adı/takma adıa\n" \
               "[34;1m{requester.id}[0m -> Şarkıyı isteyen üyenin kimliği```\n" \
               "Örnek: Oynamak {track.title} - {track.author}\n\n" \
               "`Not: Ses kanalında durum mesajında ​​özel emojiler kullanabilirsiniz (bulunmadığım sunuculardaki ve bulunmadığınız sunuculardaki emojiler dahil).`"

    def __init__(self, ctx: Union[CustomContext, disnake.Interaction], bot: BotCore, guild: disnake.Guild, data: dict):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.bot = bot
        self.data = data
        self.guild = guild
        self.message = None

        if self.data['voice_channel_status']:
            setstatus_btn = disnake.ui.Button(label="Mevcut şablonu kullan", emoji="🎶",
                                              style=disnake.ButtonStyle.grey,
                                              custom_id="status_voice_channel_temp_current")
            setstatus_btn.callback = self.setstatus_callback
            self.add_item(setstatus_btn)

        setstatus_modal_btn = disnake.ui.Button(label="Durumu Etkinleştir/Devre Dışı Bırak",
                                                emoji='🔊', style=disnake.ButtonStyle.grey)
        setstatus_modal_btn.callback = self.set_status_modal
        self.add_item(setstatus_modal_btn)

        setstatus_perm_btn = disnake.ui.Button(label="Etkinleştirme/Devre Dışı Bırakma durumu (kalıcı)",
                                               emoji='💾', style=disnake.ButtonStyle.grey)
        setstatus_perm_btn.callback = self.set_status_perm
        self.add_item(setstatus_perm_btn)


    async def setstatus_callback(self, interaction: disnake.MessageInteraction):
        await self.modal_handler(inter=interaction, values={"status_voice_value": self.data['voice_channel_status']})


    async def set_status_modal(self, interaction: disnake.MessageInteraction):

        await interaction.response.send_modal(
            ViewModal(
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="status",
                        custom_id="status_voice_value",
                        placeholder="Devre dışı bırakmak için boş bırakın",
                        max_length=496,
                        required=False
                    ),
                ],
                view=self,
                title="Kanal durumunu ayarla",
                custom_id="status_voice_channel_temp",
            )
        )


    async def set_status_perm(self, interaction: disnake.MessageInteraction):

        await interaction.response.send_modal(
            ViewModal(
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="kalıcı durum",
                        custom_id="status_voice_value",
                        placeholder="Pra desativar deixe vazio",
                        max_length=496,
                        required=False
                    ),
                ],
                view=self,
                title="Kanal durumunu ayarla",
                custom_id="status_voice_channel_perm",
            )
        )

    def build_embeds(self):

        embeds = []

        color = self.bot.get_color(self.guild.me)

        embeds.append(
            disnake.Embed(
                description="### Ses kanalında veya sahnede otomatik durumu ayarlayın\n"
                            "**Yer tutucular:**`(Durum mesajına en az bir yer tutucu eklenmelidir)`\n"
                            f"{self.placeholder_text}",
                color=color)
        )

        if self.data['voice_channel_status']:
            embeds.append(
                disnake.Embed(title="**Mevcut kalıcı model:**", description=self.data['voice_channel_status'], color=self.bot.get_color(self.guild.me))
            )

        return embeds

    async def modal_handler(self, inter: disnake.ModalInteraction, values: dict = None):

        try:
            values = inter.text_values
        except AttributeError:
            pass

        values["status_voice_value"] = values["status_voice_value"].replace("\n", " ").strip()

        if values["status_voice_value"] and not any(
                p in values["status_voice_value"] for p in self.placeholders):
            await inter.send("**En az bir geçerli yer tutucu kullanmalısınız...**", ephemeral=True)
            return

        if inter.data.custom_id == "status_voice_channel_perm":

            if self.data["voice_channel_status"] == values["status_voice_value"]:
                await inter.send("**Şu anki kalıcı durumu bildirilenle aynı...**", ephemeral=True)
                return

            guild: Optional[disnake.Guild] = None

            for b in self.bot.pool.get_guild_bots(inter.guild_id):
                if (guild:=b.get_guild(inter.guild_id)):
                    break

            if not guild:
                await inter.send("**Sunucuda kullanılabilir bot bulunmamaktadır, Aşağıdaki butona tıklayarak en az bir tane ekleyin.**",
                                components=[disnake.ui.Button(custom_id="bot_invite", label="Adicionar bots")], ephemeral=True)
                return

            inter.author = guild.get_member(inter.author.id)

            if not inter.author.guild_permissions.manage_guild:
                await inter.send("**Ses kanalı durumunu değiştirmek için Sunucuyu Yönetme izniniz yok**", ephemeral=True)
                return

            self.data["voice_channel_status"] = values["status_voice_value"]

            await inter.response.defer(ephemeral=True)

            await self.bot.update_global_data(inter.guild_id, self.data, db_name=DBModel.guilds)

            for b in self.bot.pool.get_guild_bots(inter.guild_id):
                try:
                    p = b.music.players[inter.guild_id]
                except KeyError:
                    continue
                p.stage_title_event = bool(values["status_voice_value"])
                p.stage_title_template = values["status_voice_value"]
                p.start_time = disnake.utils.utcnow()
                p.set_command_log(
                    text=f"{inter.author.mention} " + ("Aktif" if values["status_voice_value"] else "engelli") + " otomatik durum",
                    emoji="📢",
                )
                p.update = True

                if p.stage_title_event:
                    await p.update_stage_topic()
                else:
                    await p.update_stage_topic(clear=True)

                await p.process_save_queue()
                await asyncio.sleep(3)

            await inter.edit_original_message("**Kalıcı statü (önceki değeri) " + ("kaydedildi" if values["status_voice_value"] else "engelli") + " başarıyla!**" )

        elif inter.data.custom_id.startswith("status_voice_channel_temp"):

            player: Optional[LavalinkPlayer] = None

            for bot in self.bot.pool.get_guild_bots(inter.guild_id):
                try:
                    player = bot.music.players[inter.guild_id]
                except KeyError:
                    continue

                if inter.author.id not in player.guild.me.voice.channel.voice_states:
                    continue

                break

            if not player:
                await inter.send("**Bir ses/sahne kanalında müzik çalmıyorum...**", ephemeral=True)
                return

            inter.author = player.guild.get_member(inter.author.id)

            if not inter.author.guild_permissions.manage_guild:
                await inter.send("Ses kanalı durumunu değiştirmek için Sunucuyu Yönetme izniniz yok", ephemeral=True)
                return

            player.stage_title_event = bool(values["status_voice_value"])
            player.stage_title_template = values["status_voice_value"]
            player.start_time = disnake.utils.utcnow()

            await inter.response.defer(ephemeral=True)

            if player.stage_title_event:
                await player.update_stage_topic()
            else:
                await player.update_stage_topic(clear=True)

            await player.process_save_queue()

            player.set_command_log(
                text=f"{inter.author.mention} " + ("Aktif" if values["status_voice_value"] else "engelli") + " otomatik durum",
                emoji="📢",
            )

            player.update = True

            await inter.edit_original_message("**Durum başarıyla ayarlandı!**" if values["status_voice_value"] else "**Başarıyla devre dışı bırakma durumu!**")

        else:
            await inter.send(f"Uygulanmadı: {inter.data.custom_id}", ephemeral=True)
            return

        await self.close()
        self.stop()

    async def on_timeout(self) -> None:
        await self.close()

    async def close(self):

        for c in self.children:
            c.disabled = True

        if isinstance(self.ctx, CustomContext):
            try:
                await self.message.edit(view=self)
            except:
                pass
        else:
            try:
                await self.ctx.edit_original_message(view=self)
            except:
                pass

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.ctx.author.id:
            await inter.send(f"Sadece üyeler {self.ctx.author.mention} bu mesajla etkileşime girebilir.",
                             ephemeral=True)
            return False
        return True


class SkinEditorMenu(disnake.ui.View):

    def __init__(self, ctx: Union[CustomContext, disnake.AppCmdInter], bot: BotCore, guild: disnake.Guild, global_data: dict):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.bot = bot
        self.guild = guild
        self.message: Optional[disnake.Message] = None
        self.embed_index = 0
        self.embed_field_index = 0
        self.mode: Literal["editor", "select"] = "select"
        self.global_data = global_data
        self.skin_selected = ""
        self.message_data = {}
        self.update_components()

    def disable_buttons(self):
        for c in self.children:
            if c.custom_id != "skin_editor_placeholders":
                c.disabled = True

    async def new_skin(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.message_data = deepcopy(base_skin)
        self.mode = "editor"
        self.update_components()
        await self.update_message(inter)

    async def load_skin(self, inter: disnake.MessageInteraction):

        self.ctx = inter
        self.skin_selected = inter.values[0]
        self.mode = "editor"

        if self.skin_selected.startswith("> cs: "):
            skin_data = self.global_data["custom_skins"][self.skin_selected[6:]]
        elif self.skin_selected.startswith("> css: "):
            skin_data = self.global_data["custom_skins_static"][self.skin_selected[7:]]
        else:
            skin_data = None

        if isinstance(skin_data, str):
            self.message_data = pickle.loads(b64decode(skin_data))
        elif not skin_data:
            self.message_data = deepcopy(base_skin)
        else:
            self.message_data = skin_data

        self.update_components()
        await self.update_message(inter)

    def update_components(self):

        self.clear_items()

        if self.mode == "select":
            add_skin_prefix = (lambda d: [f"> cs: {i}" for i in d.keys()])
            skins_opts = [disnake.SelectOption(emoji="💠", label=f"Modo normal: {s.replace('> cs: ', '', 1)}", value=s) for s in add_skin_prefix(self.global_data["custom_skins"])]
            add_skin_prefix = (lambda d: [f"> css: {i}" for i in d.keys()])
            static_skins_opts = [disnake.SelectOption(emoji="💠", label=f"Song-Request: {s.replace('> css: ', '', 1)}", value=s) for s in add_skin_prefix(self.global_data["custom_skins_static"])]

            has_skins = False

            if skins_opts:
                skin_select = disnake.ui.Select(min_values=1, max_values=1, options=skins_opts,
                                                placeholder="Görünümler normal oyuncu modunu yapar")
                skin_select.callback = self.load_skin
                self.add_item(skin_select)
                has_skins = True

            if static_skins_opts:
                static_skin_select = disnake.ui.Select(min_values=1, max_values=1, options=static_skins_opts,
                                                       placeholder="Görünümler oynatıcının şarkı isteğini modo yapar")
                static_skin_select.callback = self.load_skin
                self.add_item(static_skin_select)
                has_skins = True

            if not has_skins:
                self.message_data = {"embeds": [{"description": "**Kaydedilmiş dış görünüm yok...\nYeni bir dış görünüm/şablon oluşturmak için aşağıdaki düğmeyi tıklayın.**", "color": self.guild.me.color.value}]}
                new_skin_btn = disnake.ui.Button(label="Yeni görünüm ekle", custom_id="skin_editor_new_skin", disabled=len(static_skins_opts) > 2 and len(skins_opts) > 2)
                new_skin_btn.callback = self.new_skin
                self.add_item(new_skin_btn)
            else:
                self.message_data = {"embeds": [{"description": "**Düzenlemek için aşağıdan bir görünüm seçin veya aşağıdaki ekle düğmesini tıklayarak temel şablon kullanarak yeni bir görünüm oluşturun.**", "color": self.guild.me.color.value}]}
                new_skin_btn = disnake.ui.Button(label="Yeni görünüm ekle", custom_id="skin_editor_new_skin", disabled=len(static_skins_opts) > 2 and len(skins_opts) > 2)
                new_skin_btn.callback = self.new_skin
                self.add_item(new_skin_btn)

        elif self.mode == "editor":

            if embeds:=self.message_data.get("embeds"):

                select_embed = disnake.ui.Select(
                    min_values = 1, max_values = 1, options=[
                        disnake.SelectOption(label=f"Embed {n+1}", value=f"skin_embed_{n}", default=n == self.embed_index) for n, e in enumerate(embeds)
                    ]
                )

                select_embed.callback = self.embed_select_callback
                self.add_item(select_embed)

                if fields:=embeds[self.embed_index].get("fields", []):
                    select_embed_field = disnake.ui.Select(
                        min_values=1, max_values=1, options=[
                            disnake.SelectOption(label=f"Field {n + 1}", value=f"skin_embed_field_{n}", default=n == self.embed_field_index) for n, e in enumerate(fields)
                        ]
                    )
                    select_embed_field.callback = self.embed_value_select_callback
                    self.add_item(select_embed_field)

                if len(fields) < 25:
                    add_field_btn = disnake.ui.Button(label="Alan ekle", emoji="🔖")
                    add_field_btn.callback = self.add_field
                    self.add_item(add_field_btn)

                if fields:
                    edit_field_btn = disnake.ui.Button(label="Alanı Düzenle", emoji="🔖")
                    edit_field_btn.callback = self.edit_embed_field_button
                    self.add_item(edit_field_btn)

                    delete_field_btn = disnake.ui.Button(label="Alan Kaldır", emoji="🔖")
                    delete_field_btn.callback = self.delete_embed_field_button
                    self.add_item(delete_field_btn)

                edit_embed_btn = disnake.ui.Button(label="Embed Düzenle", emoji="📋")
                edit_embed_btn.callback = self.edit_embed_button
                self.add_item(edit_embed_btn)

                remove_embed_btn = disnake.ui.Button(label="Embed Kaldır", emoji="📋")
                remove_embed_btn.callback = self.remove_embed
                self.add_item(remove_embed_btn)

                set_author_footer_btn = disnake.ui.Button(label="Yazar + Alt Bilgiyi Yerleştir", emoji="👤")
                set_author_footer_btn.callback = self.set_author_footer
                self.add_item(set_author_footer_btn)

            edit_content_btn = disnake.ui.Button(label=("Eklemek" if not self.message_data.get("content") else "Editar") + " Mensagem", emoji="💬")
            edit_content_btn.callback = self.edit_content
            self.add_item(edit_content_btn)

            add_embed_btn = disnake.ui.Button(label="Embed Ekle", disabled=len(embeds)>=8, emoji="📋")
            add_embed_btn.callback = self.add_embed
            self.add_item(add_embed_btn)

            setup_queue_btn = disnake.ui.Button(label="Yer tutucuyu yapılandır kuyruktan", emoji="<:music_queue:703761160679194734>")
            setup_queue_btn.callback = self.setup_queue
            self.add_item(setup_queue_btn)

            save_disabled = not embeds and len(self.message_data.get("content", "")) < 15

            export_btn = disnake.ui.Button(label="Görünümü Dışa Aktar", emoji="📤", disabled=save_disabled)
            export_btn.callback = self.export
            self.add_item(export_btn)

            import_btn = disnake.ui.Button(label="Görünümü İçe Aktar", emoji="📥")
            import_btn.callback = self.import_
            self.add_item(import_btn)

            if self.skin_selected:
                delete_skin_btn = disnake.ui.Button(label="Dış Görünüm", emoji="🚮")
                delete_skin_btn.callback = self.delete_skin
                self.add_item(delete_skin_btn)

            back_btn = disnake.ui.Button(label="Önceki menüye dön", emoji="⬅️")
            back_btn.callback = self.back
            self.add_item(back_btn)

            self.add_item(disnake.ui.Button(label="Yer tutucu listesi", emoji="<:help:947781412017279016>", custom_id="skin_editor_placeholders"))

            save_btn = disnake.ui.Button(label="Görünümü Kaydet", emoji="💾", disabled=save_disabled)
            save_btn.callback = self.save
            self.add_item(save_btn)

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.ctx.author.id:
            await inter.send(f"Bu mesajla yalnızca {self.ctx.author.mention} üyesi etkileşim kurabilir.",
                             ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:

        self.disable_buttons()

        if isinstance(self.ctx, CustomContext):
            try:
                await self.message.edit(view=self)
            except:
                pass
        else:
            try:
                await self.ctx.edit_original_message(view=self)
            except:
                pass

    def build_embeds(self) -> dict:

        player = None
        for b in self.bot.pool.get_guild_bots(self.ctx.guild_id):
            try:
                player = b.music.players[self.ctx.guild_id]
                break
            except KeyError:
                continue

        data = skin_converter(self.message_data, guild=self.guild, ctx=self.ctx, player=player)
        return {"content": data.get("content", ""), "embeds": data.get("embeds", [])}

    async def embed_select_callback(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.embed_index = int(inter.values[0][11:])
        await inter.response.defer()

    async def embed_value_select_callback(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.embed_field_index = int(inter.values[0][17:])
        await inter.response.defer()

    async def edit_content(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self, title="Mesaj içeriğini düzenleyin/ekleyin", custom_id="skin_editor_message_content",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Mensagem:",
                        custom_id="message_content",
                        value=self.message_data.get("content", ""),
                        max_length=1700,
                        required=False
                    ),
                ]
            )
        )

    async def add_embed(self, inter: disnake.MessageInteraction):
        self.ctx = inter

        await inter.response.send_modal(
            ViewModal(
                view=self, title="Embed Ekle", custom_id="skin_editor_add_embed",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Embed İsmi:",
                        custom_id="skin_embed_title",
                        max_length=170,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Embed Açıklaması:",
                        custom_id="skin_embed_description",
                        max_length=1700,
                        required=True
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Embed Rengi:",
                        placeholder="Örnek: #000fff ou {guild.color}",
                        custom_id="skin_embed_color",
                        max_length=15,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Resim bağlantısı Embed:",
                        custom_id="image_url",
                        max_length=400,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Embed Thumbnail:",
                        custom_id="thumbnail_url",
                        max_length=400,
                        required=False
                    ),
                ]
            )
        )

    async def edit_embed_button(self, inter: disnake.MessageInteraction):

        self.ctx = inter

        embed = self.message_data["embeds"][self.embed_index]

        try:
            image_url = embed["image"]["url"]
        except KeyError:
            image_url = ""

        try:
            thumb_url = embed["thumbnail"]["url"]
        except KeyError:
            thumb_url = ""

        await inter.response.send_modal(
            ViewModal(
                view=self, title="Ana yerleştirme alanlarını düzenleyin", custom_id="skin_editor_edit_embed",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Embed Title:",
                        custom_id="skin_embed_title",
                        value=embed.get("title", ""),
                        max_length=170,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Embed Açıklaması:",
                        custom_id="skin_embed_description",
                        value=embed.get("description", ""),
                        max_length=1700,
                        required=True
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Embed Color:",
                        placeholder="Örnek: #000fff ou {guild.color}",
                        custom_id="skin_embed_color",
                        value=str(embed.get("color", "")),
                        max_length=14,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Embed Resmi:",
                        custom_id="image_url",
                        value=image_url,
                        max_length=400,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Embed Thumbnail:",
                        custom_id="thumbnail_url",
                        value=thumb_url,
                        max_length=400,
                        required=False
                    ),
                ]
            )
        )

    async def remove_embed(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        del self.message_data["embeds"][self.embed_index]
        self.embed_index = 0
        self.update_components()
        await inter.response.edit_message(view=self, **self.build_embeds())

    async def add_field(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self, title="Yerleştirilecek alanı ekleyin", custom_id="skin_editor_add_field",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="İsim:",
                        custom_id="add_field_name",
                        max_length=170,
                        required=True
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Değer/Metin:",
                        custom_id="add_field_value",
                        max_length=1700,
                        required=True
                    ),
                ]
            )
        )

    async def edit_embed_field_button(self, inter: disnake.MessageInteraction):

        self.ctx = inter

        field = self.message_data["embeds"][self.embed_index]["fields"][self.embed_field_index]

        await inter.response.send_modal(
            ViewModal(
                view=self, title="Ana yerleştirme alanlarını düzenleme", custom_id="skin_editor_edit_field",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Alan adı:",
                        custom_id="edit_field_name",
                        value=field["name"],
                        max_length=170,
                        required=True
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Alan Değeri/Metin:",
                        custom_id="edit_field_value",
                        value=field["value"],
                        max_length=1700,
                        required=True
                    ),
                ]
            )
        )

    async def delete_embed_field_button(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        del self.message_data["embeds"][self.embed_index]["fields"][self.embed_field_index]
        self.embed_field_index = 0
        self.update_components()
        await inter.response.edit_message(view=self, **self.build_embeds())

    async def set_author_footer(self, inter: disnake.MessageInteraction):

        self.ctx = inter

        try:
            author_name = self.message_data["embeds"][self.embed_index]["author"]["name"]
        except KeyError:
            author_name = ""

        try:
            author_url = self.message_data["embeds"][self.embed_index]["author"]["url"]
        except KeyError:
            author_url = ""

        try:
            author_icon_url = self.message_data["embeds"][self.embed_index]["author"]["icon_url"]
        except KeyError:
            author_icon_url = ""

        try:
            footer_text = self.message_data["embeds"][self.embed_index]["footer"]["text"]
        except KeyError:
            footer_text = ""

        try:
            footer_icon_url = self.message_data["embeds"][self.embed_index]["footer"]["icon_url"]
        except KeyError:
            footer_icon_url = ""

        await inter.response.send_modal(
            ViewModal(
                view=self, custom_id="skin_editor_set_authorfooter", title="Adicionar/editar autor/footer",
                components = [
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Yazarın ismi:",
                        custom_id="set_author_name",
                        value=author_name,
                        max_length=170,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Yazarın bağlantısı/URL'si:",
                        custom_id="set_author_url",
                        value=author_url,
                        max_length=400,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Yazar resmi bağlantısı/url'si:",
                        custom_id="set_author_icon",
                        value=author_icon_url,
                        max_length=400,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Altbilgi metni:",
                        custom_id="footer_text",
                        value=footer_text,
                        max_length=1700,
                        required=False
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Altbilgi resminin URL'si/Bağlantısı:",
                        custom_id="footer_icon_url",
                        value=footer_icon_url,
                        max_length=400,
                        required=False
                    ),
                ]
            )
        )

    async def setup_queue(self, inter: disnake.MessageInteraction):

        self.ctx = inter

        await inter.response.send_modal(
            ViewModal(
                view=self, title="Sıra oynatma listesi yer tutucusu", custom_id="skin_editor_setup_queue",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Şarkı adlarındaki metin biçimlendirmesi:",
                        custom_id="queue_format",
                        value=self.message_data["queue_format"],
                        max_length=120,
                        required=True
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Listede görüntülenen şarkı sayısı:",
                        custom_id="queue_max_entries",
                        value=str(self.message_data["queue_max_entries"]),
                        max_length=2,
                        required=True
                    ),
                ]
            )
        )

    async def export(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        fp = BytesIO(bytes(json.dumps(self.message_data, indent=4), 'utf-8'))
        await inter.response.send_message(file=disnake.File(fp=fp, filename="skin.json"), ephemeral=True)

    async def import_(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self, title="Görünümü içe aktar", custom_id="skin_editor_import_skin",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Dış görünüm kodu (json):",
                        custom_id="skin",
                        max_length=2000,
                        required=True
                    )
                ]
            )
        )

    async def save(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self, title="Bilgisiz veya deriden isim", custom_id="skin_editor_save",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="İsim:",
                        custom_id="skin_name",
                        value=self.skin_selected.replace("> css: ", "", 1).replace("> cs: ", "", 1),
                        max_length=15,
                        required=True
                    )
                ]
            )
        )

    async def delete_skin(self, inter: disnake.MessageInteraction):

        self.ctx = inter

        await inter.response.defer()

        self.global_data = await self.bot.get_global_data(id_=inter.guild_id, db_name=DBModel.guilds)

        if self.skin_selected.startswith("> cs:"):
            try:
                del self.global_data["custom_skins"][self.skin_selected[6:]]
            except KeyError:
                await inter.send(f'**{self.skin_selected[6:]} dış görünümü artık veritabanında mevcut değil..**', ephemeral=True)
                return

        elif self.skin_selected.startswith("> css:"):
            try:
                del self.global_data["custom_skins_static"][self.skin_selected[7:]]
            except KeyError:
                await inter.send(f'**{self.skin_selected[7:]} dış görünümü artık veritabanında mevcut değil...**', ephemeral=True)
                return

        await self.bot.update_global_data(id_=inter.guild_id, data=self.global_data, db_name=DBModel.guilds)

        self.mode = "select"
        self.skin_selected = ""
        self.update_components()

        await inter.edit_original_message(view=self, **self.build_embeds())

    async def back(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.mode = "select"
        self.skin_selected = ""
        self.message_data = {}
        self.update_components()
        await self.update_message(inter)

    async def update_message(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        try:
            if isinstance(self.ctx, CustomContext):
                await inter.response.edit_message(view=self, **self.build_embeds())
            elif not inter.response.is_done():
                await inter.response.edit_message(view=self, **self.build_embeds())
            else:
                await inter.edit_original_message(view=self, **self.build_embeds())
        except Exception as e:
            traceback.print_exc()
            await inter.send(f"**Mesaj işlenirken bir hata oluştu:** ```py\n{repr(e)}```", ephemeral=True)

    async def modal_handler(self, inter: disnake.ModalInteraction):

        if inter.custom_id == "skin_editor_message_content":
            self.ctx = inter
            self.message_data["content"] = inter.text_values["message_content"]

        elif inter.custom_id == "skin_editor_add_embed":
            self.ctx = inter

            e = disnake.Embed(
                title=inter.text_values["skin_embed_title"],
                description=inter.text_values["skin_embed_description"],
            ).set_image(url=inter.text_values["image_url"]).set_thumbnail(inter.text_values["thumbnail_url"]).\
                to_dict()

            e["color"] = inter.text_values["skin_embed_color"].strip("#")

            self.message_data["embeds"].append(e)
            self.embed_index = len(self.message_data["embeds"]) - 1

        elif inter.custom_id == "skin_editor_edit_embed":

            self.ctx = inter

            self.message_data["embeds"][self.embed_index]["title"] = inter.text_values["skin_embed_title"]
            self.message_data["embeds"][self.embed_index]["description"] = inter.text_values["skin_embed_description"]

            if not inter.text_values["image_url"]:
                try:
                    del self.message_data["embeds"][self.embed_index]["image"]
                except KeyError:
                    pass
            else:
                self.message_data["embeds"][self.embed_index]["image"] = {"url": inter.text_values["image_url"]}

            if not inter.text_values["thumbnail_url"]:
                try:
                    del self.message_data["embeds"][self.embed_index]["thumbnail"]
                except KeyError:
                    pass
            else:
                self.message_data["embeds"][self.embed_index]["thumbnail"] = {"url": inter.text_values["thumbnail_url"]}

            self.message_data["embeds"][self.embed_index]["color"] = inter.text_values["skin_embed_color"].strip("#")

        elif inter.custom_id == "skin_editor_add_field":

            self.ctx = inter

            if not self.message_data["embeds"][self.embed_index].get("fields"):
                self.message_data["embeds"][self.embed_index]["fields"] = [{"name": inter.text_values["add_field_name"], "value": inter.text_values["add_field_value"]}]
            else:
                self.message_data["embeds"][self.embed_index]["fields"].append({"name": inter.text_values["add_field_name"], "value": inter.text_values["add_field_value"]})

            self.embed_field_index = len(self.message_data["embeds"][self.embed_index]["fields"]) - 1

        elif inter.custom_id == "skin_editor_edit_field":
            self.ctx = inter
            self.message_data["embeds"][self.embed_index]["fields"][self.embed_field_index] = {"name":inter.text_values["edit_field_name"], "value":inter.text_values["edit_field_value"]}

        elif inter.custom_id == "skin_editor_set_authorfooter":

            self.ctx = inter

            if not inter.text_values["footer_text"]:
                try:
                    del self.message_data["embeds"][self.embed_index]["footer"]
                except KeyError:
                    pass
            else:
                self.message_data["embeds"][self.embed_index]["footer"] = {
                    "text": inter.text_values["footer_text"],
                    "icon_url": inter.text_values["footer_icon_url"]
                }

            if not inter.text_values["set_author_name"]:
                try:
                    del self.message_data["embeds"][self.embed_index]["author"]
                except KeyError:
                    pass
            else:
                self.message_data["embeds"][self.embed_index]["author"] = {
                    "name": inter.text_values["set_author_name"],
                    "url": inter.text_values["set_author_url"],
                    "icon_url": inter.text_values["set_author_icon"],
                }

        elif inter.custom_id == "skin_editor_setup_queue":
            self.ctx = inter
            self.message_data["queue_format"] = inter.text_values["queue_format"]
            try:
                self.message_data["queue_max_entries"] = int(inter.text_values["queue_max_entries"])
            except TypeError:
                pass

        elif inter.custom_id == "skin_editor_import_skin":

            self.ctx = inter

            try:
                info = json.loads(inter.text_values["skin"])
            except Exception as e:
                await inter.send(f"**Cildiniz işlenirken bir hata oluştu:** ```py\n{repr(e)}```", ephemeral=True)
                return

            try:
                if len(str(info["queue_max_entries"])) > 2:
                    info["queue_max_entries"] = 7
            except:
                pass

            try:
                if not isinstance(info["queue_format"], str):
                    info["queue_format"] = self.message_data["queue_format"]
            except KeyError:
                pass

            try:
                self.message_data["embeds"] = info["embeds"]
            except KeyError:
                pass
            try:
                self.message_data["content"] = info["content"]
            except KeyError:
                pass
            try:
                self.message_data["queue_format"] = info["queue_format"]
            except KeyError:
                pass
            try:
                self.message_data["queue_max_entries"] = info["queue_max_entries"]
            except KeyError:
                pass

            self.embed_index = 0
            self.embed_field_index = 0

        elif inter.custom_id == "skin_editor_save":

            view = SkinSettingsButton(self.ctx.author, timeout=30)
            view.controller_enabled = self.message_data.get("controller_enabled", True)
            await inter.send("**Dış görünüme uygulanacak oynatıcı modunu seçin.**", view=view, ephemeral=True)
            await view.wait()

            if view.mode is None:
                await inter.edit_original_message("Tempo esgotado!", components=[])
                return

            self.message_data["controller_enabled"] = view.controller_enabled

            if view.inter:
                await view.inter.response.defer(ephemeral=True)

            self.global_data = await self.bot.get_global_data(self.ctx.guild_id, db_name=DBModel.guilds)

            modal_skin_name = inter.text_values["skin_name"].strip()

            skin_name = self.skin_selected.replace("> css: ", "", 1).replace("> cs: ", "", 1)

            if modal_skin_name != skin_name:
                try:
                    del self.global_data[view.mode][skin_name]
                except KeyError:
                    pass

            self.global_data[view.mode][modal_skin_name] = b64encode(pickle.dumps(self.message_data)).decode('utf-8')

            await self.bot.update_global_data(id_=inter.guild_id, data=self.global_data, db_name=DBModel.guilds)

            for bot in self.bot.pool.get_guild_bots(inter.guild_id):

                try:
                    player = bot.music.players[inter.guild_id]
                except KeyError:
                    continue

                global_data = self.global_data.copy()

                for n, s in global_data["custom_skins"].items():
                    if isinstance(s, str):
                        global_data["custom_skins"][n] = pickle.loads(b64decode(s))

                for n, s in global_data["custom_skins_static"].items():
                    if isinstance(s, str):
                        global_data["custom_skins_static"][n] = pickle.loads(b64decode(s))

                player.custom_skin_data = global_data["custom_skins"]
                player.custom_skin_static_data = global_data["custom_skins_static"]
                player.setup_features()
                player.setup_hints()
                player.process_hint()

            try:
                cmd = f"</change_skin:" + str(self.bot.get_global_command_named("change_skin",
                                                                                             cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
            except AttributeError:
                cmd = "/change_skin"

            try:
                guild_prefix = self.bot.pool.guild_prefix_cache[self.ctx.guild_id]
            except KeyError:
                guild_prefix = self.global_data.get("prefix")

            if not guild_prefix:
                guild_prefix = self.bot.config.get("DEFAULT_PREFIX") or "!!"

            if not view.inter:
                view.inter = inter

            await view.inter.edit_original_message("**Görünüm başarıyla kaydedildi/düzenlendi!**\n"
                                                   f"{cmd} veya {guild_prefix}skin komutunu kullanarak uygulayabilirsiniz",
                                                   view=None)

            self.skin_selected = ("> cs: " if view.mode == "custom_skins" else "> css: ") + modal_skin_name

            self.update_components()

            if isinstance(self.ctx, CustomContext):
                await self.message.edit(view=self, **self.build_embeds())
            else:
                await self.ctx.edit_original_message(view=self, **self.build_embeds())
            return

        self.update_components()
        await self.update_message(inter)

class SelectBotVoice(disnake.ui.View):

    def __init__(
            self,
            inter: Union[disnake.AppCmdInter, disnake.MessageInteraction, disnake.ModalInteraction, CustomContext],
            guild: disnake.Guild, freebots: List[BotCore]
    ):
        super().__init__(timeout=45)
        self.inter = inter
        self.message: Optional[disnake.Message] = None
        self.voice_channel = inter.author.voice.channel
        self.guild = guild
        self.build_interactions(freebots)
        self.bot: Optional[BotCore] = None
        self.status = None

    def build_interactions(self, freebots: List[BotCore] = None):

        self.clear_items()

        bot_select_opts = []

        if freebots:
            bot_select_opts.extend([disnake.SelectOption(label=b.user.display_name, value=f"bot_voice_{b.user.id}") for b in freebots])

        else:
            for b in self.inter.bot.pool.get_guild_bots(self.guild.id):

                if not b.bot_ready:
                    continue

                guild = b.get_guild(self.inter.guild_id)

                if not guild:
                    continue

                player: LavalinkPlayer = b.music.players.get(self.inter.guild_id)

                if player and self.inter.author.id not in player.last_channel.voice_states:
                    continue

                bot_select_opts.append(disnake.SelectOption(label=b.user.display_name, value=f"bot_voice_{b.user.id}"))

        if not bot_select_opts:
            self.status = False
            self.stop()
            return

        bot_select = disnake.ui.Select(min_values=0, max_values=1, options=bot_select_opts)
        bot_select.callback = self.bot_select_callback
        self.add_item(bot_select)

        refresh_btn = disnake.ui.Button(label="Güncelleme listesi", emoji="🔄", style=disnake.ButtonStyle.blurple)
        refresh_btn.callback = self.reload_callback
        self.add_item(refresh_btn)

        cancel_btn = disnake.ui.Button(label="İptal", emoji="❌")
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    async def update_message(self):
        self.build_interactions()
        if self.message:
            await self.message.edit(view=self)
        else:
            await self.inter.edit_original_message(view=self)

    async def reload_callback(self, inter: disnake.MessageInteraction):
        self.build_interactions()
        await inter.response.edit_message(view=self)

    async def cancel_callback(self, inter: disnake.MessageInteraction):
        self.status = False
        self.inter = inter
        self.stop()

    async def bot_select_callback(self, inter: disnake.MessageInteraction):

        bot_id = int(inter.values[0][10:])

        try:
            bot = [b for b in self.inter.bot.pool.get_guild_bots(inter.guild_id) if b.bot_ready and b.user.id == bot_id][0]
        except IndexError:
            await inter.send(f"<@{bot_id}> artık sunucuda mevcut değil...", ephemeral=True)
            await self.update_message()
            return

        guild = bot.get_guild(inter.guild_id)

        if not guild:
            await inter.send(f"{bot.user.mention} Artık sunucuda değil...", ephemeral=True)
            await self.update_message()
            return

        player = bot.music.players.get(inter.guild_id)

        if player:

            try:
                vc = player.guild.me.voice.channel
            except AttributeError:
                vc = player.last_channel

            if not vc:
                await inter.send(
                    f"{bot.user.mention} oynatıcı aktif ancak bir ses kanalına bağlı değil...",
                    ephemeral=True)
                await self.update_message()
                return

            if inter.author.id not in vc.voice_states:
                await inter.send(f"{bot.user.mention} zaten {vc.mention} kanalında kullanılıyor", ephemeral=True)
                await self.update_message()
                return

        inter.author = guild.get_member(inter.author.id)

        try:
            inter.music_guild = guild
            inter.music_bot = bot
        except AttributeError:
            pass

        self.bot = bot
        self.inter = inter
        self.guild = guild
        self.status = True
        self.stop()
