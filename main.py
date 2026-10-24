from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("STRYKE_DATA_DIR", "").strip() or str(ROOT)).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CAPTURE_MANUAL_IDENTIFICATIONS = os.getenv("CAPTURE_MANUAL_IDENTIFICATIONS", "false").strip().lower() in {"1", "true", "yes", "on"}
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
META_PANEL_CHANNEL_ID = os.getenv("META_PANEL_CHANNEL_ID", "").strip()
STATE_PATH = DATA_DIR / "bot_state.json"
META_STATE_PATH = DATA_DIR / "meta_state.json"
META_GOAL_CENTS = 10_000_000
SITE_GOALS_API_URL = "https://xalxuyifedqjsuzypdvh.supabase.co/rest/v1/stryke_goals?select=name,amount,saved&order=created_at.asc"
SITE_GOALS_PUBLISHABLE_KEY = "sb_publishable_tPacfUTlkNElGsqDW4XwDg__q02zihO"
META_LOCK = asyncio.Lock()
IDENTIFICATION_LOCK = asyncio.Lock()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stryke")

PANEL_NAME = "『🤖』bot-abrir-chamado"
ABSENCE_NAME = "『😴』ᴀᴜsᴇɴᴄɪᴀs"
IDENTIFICATION_NAME = "📝 identificação"
IDENTIFICATION_STORAGE_NAME = "identificacoes"
IDENTIFICATION_STORAGE_TOPIC = "stryke:private-identifications:v1"
IDENTIFICATION_MANAGER_ROLE_NAMES = {"gerente", "gerentes", "manager", "managers"}
FARME_CATEGORY_HINTS = ("farme",)
FARME_ROLE_NAMES = ("🚀 LÍDER", "🔥 SUB LÍDER", "[RESP] FARME")
PANEL_MARKER = "stryke:permanent-ticket-panel:v1"
META_PANEL_MARKER = "stryke:family-goal-panel:v1"
IDENTIFICATION_PANEL_MARKER = "stryke:identification-panel:v1"
WEEKLY_CONTROL_MARKER = "stryke:weekly-meta-controls:v1"
META_COMPLETION_FOOTER = "STRYKE · meta familiar concluída"
SAO_PAULO_TIMEZONE = timezone(timedelta(hours=-3))


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def safe_slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text).strip("-")
    return (text[:45] or "membro")


def is_farme_ticket(channel: discord.abc.GuildChannel) -> bool:
    topic = channel.topic or "" if isinstance(channel, discord.TextChannel) else ""
    return (
        isinstance(channel, discord.TextChannel)
        and channel.name.startswith("farme-")
        and topic.startswith("STRYKE Farme ticket;")
        and re.search(r"(?:^|; )owner_id=\d+(?:;|$)", topic) is not None
    )


def identification_manager_roles(guild: discord.Guild) -> tuple[discord.Role | None, list[discord.Role]]:
    configured_ids = os.getenv("IDENTIFICATION_MANAGER_ROLE_IDS", "").strip()
    if configured_ids:
        role_ids = [value.strip() for value in re.split(r"[,;\s]+", configured_ids) if value.strip()]
        if not role_ids or any(not role_id.isdigit() for role_id in role_ids):
            return None, []
        allowed_roles = []
        for role_id in role_ids:
            role = guild.get_role(int(role_id))
            if role is None or role == guild.default_role:
                return None, []
            if role not in allowed_roles:
                allowed_roles.append(role)
        # A configured list is an exact allowlist, so every supplied role gets
        # access without granting it to unrelated roles between hierarchy levels.
        manager_role = min(allowed_roles, key=lambda role: role.position)
        return manager_role, allowed_roles

    configured_id = os.getenv("IDENTIFICATION_MANAGER_ROLE_ID", "").strip()
    if configured_id.isdigit():
        manager_role = guild.get_role(int(configured_id))
        if manager_role is None or manager_role == guild.default_role:
            return None, []
    else:
        matches = [role for role in guild.roles if norm(role.name) in IDENTIFICATION_MANAGER_ROLE_NAMES]
        if not matches:
            return None, []
        # If there are duplicate manager roles, include the full range beginning
        # at the lowest one so no manager is accidentally excluded.
        manager_role = min(matches, key=lambda role: role.position)
    allowed_roles = [
        role for role in guild.roles
        if role != guild.default_role and role.position >= manager_role.position
    ]
    return manager_role, allowed_roles


def can_review_identification(member: discord.Member) -> bool:
    _, manager_roles = identification_manager_roles(member.guild)
    return any(role in member.roles for role in manager_roles)


def identification_nickname(passport: str, name: str) -> str:
    """Put the passport before the name within Discord's 32-character nickname limit."""
    passport = passport.strip()
    name = name.strip()
    if len(passport) >= 32:
        return passport[:32]
    available_for_name = 32 - len(passport) - 3  # " | "
    if available_for_name > 0:
        return f"{passport} | {name[:available_for_name]}"
    return f"{passport} {name[:max(0, 31 - len(passport))]}"


def find_identification_storage_channel(guild: discord.Guild) -> discord.TextChannel | None:
    state = load_state().get(str(guild.id), {})
    configured_id = str(state.get("identification_storage_channel_id", "")).strip()
    if configured_id.isdigit():
        channel = guild.get_channel(int(configured_id))
        if isinstance(channel, discord.TextChannel):
            return channel
    marked = [channel for channel in guild.text_channels if channel.topic == IDENTIFICATION_STORAGE_TOPIC]
    if len(marked) == 1:
        return marked[0]
    named = [channel for channel in guild.text_channels if norm(channel.name) == norm(IDENTIFICATION_STORAGE_NAME)]
    return named[0] if len(named) == 1 else None


class StrykeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = CAPTURE_MANUAL_IDENTIFICATIONS
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(CloseTicketView())
        self.add_view(MetaTrackerView())
        self.add_view(IdentificationPanelView())
        self.add_view(IdentificationReviewView())
        self.add_view(WeeklyMetaControlView())
        if GUILD_ID.isdigit():
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Comandos sincronizados no servidor configurado (%d).", len(synced))
            # This bot is configured for one guild. Remove old global copies so
            # members don't see duplicate entries such as two /painel commands.
            self.tree.clear_commands(guild=None)
            try:
                await self.tree.sync()
                log.info("Comandos globais antigos removidos para evitar duplicidade.")
            except discord.HTTPException:
                log.exception("Não foi possível remover os comandos globais antigos.")
        else:
            synced = await self.tree.sync()
            log.info("Comandos globais sincronizados (%d). Podem levar algum tempo para aparecer.", len(synced))

    async def on_ready(self):
        if self.user:
            log.info("Conectado como %s (ID %s).", self.user.name, self.user.id)
        for guild in self.guilds:
            log.info("Servidor acessível: %s (ID %s).", guild.name, guild.id)
            persist_discovered_ids(guild)
            await self.ensure_panel(guild)
            await self.ensure_identification_panel(guild)
            storage_channel = find_identification_storage_channel(guild)
            if storage_channel is not None:
                await self.migrate_identification_nicknames(guild, storage_channel)
            await self.ensure_weekly_meta_controls(guild)
            repaired = await reconcile_farme_ticket_permissions(guild)
            if repaired:
                log.info("Permissões de acesso e histórico atualizadas em %d ticket(s) Farme.", repaired)
            self.restore_absence_cleanup(guild)
            await reconcile_meta_state(guild)
        if not refresh_site_meta_cache.is_running():
            refresh_site_meta_cache.start()

    async def on_message(self, message: discord.Message):
        if (
            message.guild is None
            or not CAPTURE_MANUAL_IDENTIFICATIONS
            or message.author.bot
            or not isinstance(message.channel, discord.TextChannel)
        ):
            return

        public_channel = find_text_channel(message.guild, IDENTIFICATION_NAME)
        if public_channel is None or message.channel.id != public_channel.id:
            return

        storage_channel = find_identification_storage_channel(message.guild)
        if storage_channel is None:
            log.error(
                "Identificação manual %s permanece no canal público porque o arquivo privado não está disponível.",
                message.id,
            )
            return

        async with IDENTIFICATION_LOCK:
            if not await self.archive_manual_identification_message(message, storage_channel):
                return
            try:
                await message.delete(reason="Guardar identificação manual no canal privado do STRYKE")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.warning(
                    "Identificação manual %s foi arquivada, mas não consegui removê-la do canal público.",
                    message.id,
                )
            except discord.HTTPException:
                log.exception(
                    "Identificação manual %s foi arquivada, mas falhei ao removê-la do canal público.",
                    message.id,
                )

    def restore_absence_cleanup(self, guild: discord.Guild) -> None:
        now = datetime.now(timezone.utc)
        for channel in guild.text_channels:
            topic = channel.topic or ""
            match = re.search(r"Canal temporário de ausência de .+ \((\d+)\); criado pelo STRYKE BOT\.", topic)
            if not match:
                continue
            created = channel.created_at
            remaining = max(0, 15 * 60 - (now - created).total_seconds())
            asyncio.create_task(delete_temporary_channel(channel, remaining))

    async def ensure_panel(self, guild: discord.Guild) -> bool:
        channel = find_text_channel(guild, PANEL_NAME)
        if channel is None:
            log.warning("Canal de painel não encontrado com nome exato: %s", PANEL_NAME)
            return False
        try:
            saved = load_state().get(str(guild.id), {}).get("panel_message_id")
            message = None
            if saved:
                try:
                    message = await channel.fetch_message(int(saved))
                except (discord.NotFound, discord.Forbidden, ValueError):
                    message = None
            if message is None:
                async for candidate in channel.history(limit=100):
                    if candidate.author == self.user and PANEL_MARKER in candidate.content:
                        message = candidate
                        break
            content = f"{PANEL_MARKER}\n## Central de atendimento STRYKE\nEscolha uma opção abaixo para continuar."
            if message:
                await message.edit(content=content, view=TicketPanelView())
            else:
                message = await channel.send(content, view=TicketPanelView())
            state = load_state()
            state.setdefault(str(guild.id), {})["panel_message_id"] = message.id
            save_state(state)
            return True
        except discord.Forbidden:
            log.error("Sem permissão para ler ou publicar no canal %s.", channel.name)
            return False
        except discord.HTTPException as exc:
            log.exception("Falha ao garantir painel: %s", exc)
            return False

    async def ensure_identification_storage_channel(
        self,
        guild: discord.Guild,
        public_channel: discord.TextChannel | None,
    ) -> discord.TextChannel | None:
        manager_role, manager_roles = identification_manager_roles(guild)
        if manager_role is None:
            log.error(
                "Cargo Gerente não encontrado; defina IDENTIFICATION_MANAGER_ROLE_ID ou crie um cargo Gerente. "
                "O canal público de identificação não será limpo."
            )
            return None
        bot_member = guild.me
        if bot_member is None:
            log.error("Não foi possível localizar o membro do bot para configurar o canal privado de identificação.")
            return None

        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for role in manager_roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=False,
            )
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            embed_links=True,
        )

        channel = find_identification_storage_channel(guild)
        if channel is None:
            named_matches = [
                candidate for candidate in guild.text_channels
                if norm(candidate.name) == norm(IDENTIFICATION_STORAGE_NAME)
            ]
            if named_matches:
                log.error(
                    "Há mais de um canal chamado %s e não consegui identificar com segurança o canal privado de identificação.",
                    IDENTIFICATION_STORAGE_NAME,
                )
                return None
        try:
            if channel is None:
                channel = await guild.create_text_channel(
                    name=IDENTIFICATION_STORAGE_NAME,
                    category=public_channel.category if public_channel else None,
                    topic=IDENTIFICATION_STORAGE_TOPIC,
                    overwrites=overwrites,
                    reason="Canal privado para guardar os cadastros de identificação do STRYKE",
                )
                log.info("Canal privado de identificações criado: %s (ID %s).", channel.name, channel.id)
            else:
                await channel.edit(
                    topic=IDENTIFICATION_STORAGE_TOPIC,
                    overwrites=overwrites,
                    reason="Restringir os cadastros de identificação a Gerente e cargos superiores",
                )
        except discord.Forbidden:
            log.exception("Sem permissão para criar ou proteger o canal privado de identificação.")
            return None
        except discord.HTTPException:
            log.exception("Falha ao criar ou atualizar o canal privado de identificação.")
            return None

        state = load_state()
        state.setdefault(str(guild.id), {})["identification_storage_channel_id"] = channel.id
        save_state(state)
        return channel

    async def archive_manual_identification_message(
        self,
        source_message: discord.Message,
        storage_channel: discord.TextChannel,
        archived_source_ids: set[str] | None = None,
    ) -> bool:
        source_id = str(source_message.id)
        if archived_source_ids is not None and source_id in archived_source_ids:
            return True

        source_marker = f"STRYKE-ID-ARCHIVE-SOURCE:{source_id}"
        if archived_source_ids is None:
            async for saved_message in storage_channel.history(limit=None):
                if any(embed.footer and embed.footer.text == source_marker for embed in saved_message.embeds):
                    return True

        if len(source_message.embeds) > 9:
            log.error("Identificação manual %s tem mais de 9 embeds; mantive a mensagem original.", source_id)
            return False

        copied_files = []
        try:
            for attachment in source_message.attachments:
                copied_files.append(await attachment.to_file(use_cached=True))

            content = source_message.content.strip()
            if content:
                description = content[:4000]
            elif source_message.stickers:
                sticker_names = ", ".join(sticker.name for sticker in source_message.stickers)
                description = f"Mensagem com sticker(s): {sticker_names}"
            else:
                description = "Mensagem enviada sem texto; consulte os anexos ou conteúdos incorporados abaixo."

            archived_embed = discord.Embed(
                title="🗂️ Identificação enviada fora do formulário",
                description=description,
                color=discord.Color.dark_teal(),
                timestamp=source_message.created_at,
            )
            archived_embed.add_field(
                name="Enviado por",
                value=f"{source_message.author.display_name} (ID {source_message.author.id})",
                inline=False,
            )
            archived_embed.add_field(
                name="Mensagem original",
                value=f"Canal #{source_message.channel.name} · ID {source_id}",
                inline=False,
            )
            archived_embed.set_footer(text=source_marker)
            copied_embeds = [embed.copy() for embed in source_message.embeds]
            await storage_channel.send(
                embeds=[archived_embed, *copied_embeds],
                files=copied_files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if archived_source_ids is not None:
                archived_source_ids.add(source_id)
            return True
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Não foi possível arquivar a identificação manual %s; a mensagem original será preservada.", source_id)
            return False

    async def archive_public_identifications(
        self,
        public_channel: discord.TextChannel,
        storage_channel: discord.TextChannel,
    ) -> bool:
        record_pattern = re.compile(r"^🪪 Cadastro de identificação #(\d+)$")
        source_pattern = re.compile(r"^STRYKE-ID-ARCHIVE-SOURCE:(\d+)$")
        archived_numbers: set[str] = set()
        archived_source_ids: set[str] = set()
        try:
            async for saved_message in storage_channel.history(limit=None):
                for embed in saved_message.embeds:
                    match = record_pattern.match(embed.title or "")
                    if match:
                        archived_numbers.add(match.group(1))
                    footer = embed.footer.text if embed.footer else ""
                    source_match = source_pattern.match(footer)
                    if source_match:
                        archived_source_ids.add(source_match.group(1))

            async for message in public_channel.history(limit=None, oldest_first=True):
                if message.author == self.user:
                    for embed in message.embeds:
                        match = record_pattern.match(embed.title or "")
                        if not match or match.group(1) in archived_numbers:
                            continue
                        # Copy each existing form registration before public cleanup.
                        await storage_channel.send(
                            embed=embed.copy(),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        archived_numbers.add(match.group(1))
                    continue
                if message.author.bot:
                    continue
                if not CAPTURE_MANUAL_IDENTIFICATIONS:
                    log.warning(
                        "Há identificações manuais no canal público; ative CAPTURE_MANUAL_IDENTIFICATIONS e Message Content Intent para arquivá-las. O canal não será limpo."
                    )
                    return False
                if not await self.archive_manual_identification_message(
                    message,
                    storage_channel,
                    archived_source_ids,
                ):
                    return False
            return True
        except discord.Forbidden:
            log.exception("Não foi possível ler ou arquivar as identificações antes da limpeza do canal público.")
            return False
        except discord.HTTPException:
            log.exception("Falha ao arquivar as identificações; as mensagens públicas serão preservadas.")
            return False

    async def migrate_identification_nicknames(
        self,
        guild: discord.Guild,
        storage_channel: discord.TextChannel,
    ) -> None:
        """Update current members from their newest approved archived identification."""
        state = load_state()
        guild_state = state.setdefault(str(guild.id), {})
        migration = guild_state.setdefault("identification_nickname_migration_v1", {"processed_user_ids": []})
        processed = set(str(user_id) for user_id in migration.get("processed_user_ids", []))
        seen: set[str] = set()
        field_pattern = re.compile(r"^🪪 Cadastro de identificação #\d+$")
        footer_pattern = re.compile(r"ID (\d+) · STRYKE ID (\d+):(\d+)$")

        try:
            async for message in storage_channel.history(limit=None):
                for embed in message.embeds:
                    if not field_pattern.match(embed.title or ""):
                        continue
                    footer = embed.footer.text if embed.footer else ""
                    match = footer_pattern.search(footer)
                    if not match:
                        continue
                    user_id = match.group(1)
                    if user_id in seen:
                        continue
                    if user_id in processed:
                        seen.add(user_id)
                        continue
                    fields = {field.name: field.value.strip() for field in embed.fields}
                    if not fields.get("Status", "").startswith("✅"):
                        continue
                    passport = fields.get("Passaporte", "")
                    name = fields.get("Nome", "")
                    if not passport or not name:
                        log.warning("Cadastro aprovado sem passaporte ou nome para o membro %s.", user_id)
                        continue
                    seen.add(user_id)
                    try:
                        member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
                        nickname = identification_nickname(passport, name)
                        if member.nick != nickname:
                            await member.edit(
                                nick=nickname,
                                reason="Atualizar apelido pelo passaporte da identificação aprovada",
                            )
                        processed.add(user_id)
                        migration["processed_user_ids"] = sorted(processed)
                        save_state(state)
                    except discord.NotFound:
                        log.info("Membro %s de identificação aprovada não está mais no servidor; apelido não atualizado.", user_id)
                    except discord.Forbidden:
                        log.warning("Sem permissão ou hierarquia para atualizar o apelido do membro %s.", user_id)
                    except discord.HTTPException:
                        log.exception("Falha ao atualizar o apelido do membro %s a partir da identificação aprovada.", user_id)
            log.info("Migração de apelidos de identificação concluída; %d membro(s) atualizado(s) ou já corretos.", len(processed))
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Não foi possível percorrer o canal de identificações para atualizar apelidos antigos.")

    async def ensure_identification_panel(self, guild: discord.Guild) -> bool:
        channel = find_text_channel(guild, IDENTIFICATION_NAME)
        storage_channel = await self.ensure_identification_storage_channel(guild, channel)
        if channel is None:
            log.warning("Canal de identificação não encontrado: %s", IDENTIFICATION_NAME)
            return False
        if storage_channel is None:
            return False
        async with IDENTIFICATION_LOCK:
            try:
                bot_member = guild.me
                if bot_member is None:
                    log.error("Não foi possível localizar o membro do bot no servidor %s.", guild.id)
                    return False
                # Grant deletion only in this one public identification channel.
                await channel.set_permissions(
                    bot_member,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=True,
                        embed_links=True,
                        manage_messages=True,
                    ),
                    reason="Permitir que o STRYKE mantenha apenas o painel público de identificação",
                )

                state = load_state()
                saved = state.get(str(guild.id), {}).get("identification_panel_message_id")
                message = None
                if saved:
                    try:
                        message = await channel.fetch_message(int(saved))
                    except (discord.NotFound, discord.Forbidden, ValueError):
                        message = None
                if message is None:
                    async for candidate in channel.history(limit=100):
                        if candidate.author == self.user and any(
                            embed.footer and embed.footer.text == IDENTIFICATION_PANEL_MARKER
                            for embed in candidate.embeds
                        ):
                            message = candidate
                            break
                embed = discord.Embed(
                    title="🪪 Cadastro de identificação",
                    description="Clique no botão abaixo e preencha seus dados. A equipe poderá consultar o cadastro no canal privado de identificações.",
                    color=discord.Color.blurple(),
                )
                embed.set_footer(text=IDENTIFICATION_PANEL_MARKER)
                if message:
                    await message.edit(embed=embed, view=IdentificationPanelView())
                else:
                    message = await channel.send(embed=embed, view=IdentificationPanelView())
                state.setdefault(str(guild.id), {})["identification_panel_message_id"] = message.id
                save_state(state)

                if not await self.archive_public_identifications(channel, storage_channel):
                    return False
                deleted = await channel.purge(
                    limit=None,
                    check=lambda candidate: candidate.id != message.id,
                    reason="Manter somente o painel público e guardar os cadastros no canal privado",
                )
                if deleted:
                    log.info("Canal público de identificação limpo; painel mantido e %d mensagens removidas.", len(deleted))
                return True
            except discord.Forbidden:
                log.exception("Sem permissão para manter o painel, arquivar cadastros ou limpar o canal %s.", channel.name)
                return False
            except discord.HTTPException:
                log.exception("Falha ao atualizar o painel de identificação no canal %s.", channel.name)
                return False

    async def ensure_weekly_meta_controls(self, guild: discord.Guild) -> bool:
        channel = find_meta_panel_channel(guild)
        if channel is None:
            log.warning("Canal de controle da meta semanal não encontrado/configurado.")
            return False
        try:
            state = load_state()
            saved = state.get(str(guild.id), {}).get("weekly_meta_controls_message_id")
            message = None
            if saved:
                try:
                    message = await channel.fetch_message(int(saved))
                except (discord.NotFound, discord.Forbidden, ValueError):
                    message = None
            if message is None:
                async for candidate in channel.history(limit=100):
                    if candidate.author == self.user and any(
                        embed.footer and embed.footer.text == WEEKLY_CONTROL_MARKER
                        for embed in candidate.embeds
                    ):
                        message = candidate
                        break
            embed = discord.Embed(
                title="⚙️ Controle da meta semanal",
                description="Use os botões para atualizar o painel público ou encerrar a semana e iniciar a próxima.",
                color=discord.Color.dark_gold(),
            )
            embed.set_footer(text=WEEKLY_CONTROL_MARKER)
            if message:
                await message.edit(embed=embed, view=WeeklyMetaControlView())
            else:
                message = await channel.send(embed=embed, view=WeeklyMetaControlView())
            state.setdefault(str(guild.id), {})["weekly_meta_controls_message_id"] = message.id
            save_state(state)
            return True
        except discord.Forbidden:
            log.error("Sem permissão para publicar os controles semanais no canal %s.", channel.name)
            return False
        except discord.HTTPException:
            log.exception("Falha ao publicar os controles semanais no canal %s.", channel.name)
            return False


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_meta_state() -> dict:
    try:
        data = json.loads(META_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"guilds": {}}
    except FileNotFoundError:
        return {"guilds": {}}
    except (OSError, json.JSONDecodeError):
        log.exception("Não foi possível ler meta_state.json; iniciando estrutura vazia em memória.")
        return {"guilds": {}}


def save_meta_state(data: dict) -> None:
    temp_path = META_STATE_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(META_STATE_PATH)


def _fetch_site_goals() -> dict:
    """Read shared goal values published by the Stryke website."""
    request = urllib.request.Request(
        SITE_GOALS_API_URL,
        headers={
            "apikey": SITE_GOALS_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {SITE_GOALS_PUBLISHABLE_KEY}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        rows = json.loads(response.read().decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("O site retornou uma lista de metas inválida.")

    total_cents = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("O site retornou um registro de meta inválido.")
        try:
            saved = Decimal(str(row.get("saved", 0)))
        except InvalidOperation as exc:
            raise ValueError("O site retornou um valor de meta inválido.") from exc
        if not saved.is_finite() or saved < 0 or saved * 100 != (saved * 100).to_integral_value():
            raise ValueError("O site retornou um valor de meta fora do formato monetário.")
        total_cents += int(saved * 100)
    return {"site_total_cents": total_cents, "site_goal_count": len(rows)}


async def fetch_site_goals() -> dict:
    return await asyncio.to_thread(_fetch_site_goals)


def store_site_goal_snapshot(guild_id: int, snapshot: dict) -> bool:
    data = load_meta_state()
    state = get_meta_guild_state(data, guild_id)
    changed = (
        state.get("site_total_cents") != snapshot["site_total_cents"]
        or state.get("site_goal_count") != snapshot["site_goal_count"]
    )
    if changed:
        state.update(snapshot)
        state["site_updated_at"] = datetime.now(timezone.utc).isoformat()
        save_meta_state(data)
    return changed


@tasks.loop(seconds=60)
async def refresh_site_meta_cache():
    snapshot = None
    try:
        snapshot = await fetch_site_goals()
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        log.exception("Não foi possível atualizar os valores do site de metas.")
    for guild in bot.guilds:
        changed = store_site_goal_snapshot(guild.id, snapshot) if snapshot is not None else False
        data = load_meta_state()
        state = get_meta_guild_state(data, guild.id)
        if sync_meta_week(state):
            save_meta_state(data)
            changed = True
        if changed:
            await refresh_meta_panel(guild.id)


@refresh_site_meta_cache.before_loop
async def before_refresh_site_meta_cache():
    await bot.wait_until_ready()


def new_meta_guild_state() -> dict:
    return {
        "cycle": 1,
        "phase": "family",
        "family_total_cents": 0,
        "family_by_member": {},
        "personal_total_cents": 0,
        "personal_by_member": {},
        "weekly_key": "",
        "weekly_total_cents": 0,
        "weekly_number": 1,
        "weekly_start_at": "",
        "weekly_end_at": "",
        "weekly_history": [],
        "submitted_by_member": {},
        "completed_cycles": [],
        "pending_family_thanks": False,
        "panel_channel_id": None,
        "panel_message_id": None,
    }


def current_meta_week_window() -> tuple[datetime, datetime]:
    """Return the current Monday 14:00 to next Monday 14:00 interval."""
    now = datetime.now(SAO_PAULO_TIMEZONE)
    monday = now.date() - timedelta(days=now.weekday())
    start = datetime.combine(monday, datetime_time(hour=14), tzinfo=SAO_PAULO_TIMEZONE)
    if now < start:
        start -= timedelta(days=7)
    return start, start + timedelta(days=7)


def sync_meta_week(state: dict) -> bool:
    """Initialize week metadata for old state without resetting an open week."""
    changed = False
    raw_start = state.get("weekly_start_at")
    raw_end = state.get("weekly_end_at")
    try:
        start = datetime.fromisoformat(raw_start) if raw_start else None
    except (TypeError, ValueError):
        start = None
    try:
        end = datetime.fromisoformat(raw_end) if raw_end else None
    except (TypeError, ValueError):
        end = None

    if start is None:
        old_key = str(state.get("weekly_key", ""))
        try:
            old_date = datetime.fromisoformat(old_key).date() if old_key else None
        except ValueError:
            old_date = None
        if old_date:
            start = datetime.combine(old_date, datetime_time(hour=14), tzinfo=SAO_PAULO_TIMEZONE)
        else:
            start, _ = current_meta_week_window()
        state["weekly_start_at"] = start.isoformat()
        changed = True
    elif start.tzinfo is None:
        start = start.replace(tzinfo=SAO_PAULO_TIMEZONE)
        state["weekly_start_at"] = start.isoformat()
        changed = True

    if end is None or end <= start:
        end = start + timedelta(days=7)
        state["weekly_end_at"] = end.isoformat()
        changed = True
    elif end.tzinfo is None:
        end = end.replace(tzinfo=SAO_PAULO_TIMEZONE)
        state["weekly_end_at"] = end.isoformat()
        changed = True

    if state.get("weekly_key") != start.date().isoformat():
        state["weekly_key"] = start.date().isoformat()
        changed = True
    if "weekly_number" not in state:
        state["weekly_number"] = 1
        changed = True
    if "weekly_total_cents" not in state:
        state["weekly_total_cents"] = 0
        changed = True
    if "weekly_history" not in state:
        state["weekly_history"] = []
        changed = True
    return changed


def weekly_period_label(state: dict) -> str:
    sync_meta_week(state)
    start = datetime.fromisoformat(state["weekly_start_at"])
    end = datetime.fromisoformat(state["weekly_end_at"])
    return format_week_dates(start, end)


def format_week_dates(start: datetime, end: datetime) -> str:
    if start.month == end.month and start.year == end.year:
        return f"{start.day} ao {end.day}/{end.month:02d}"
    return f"{start.day:02d}/{start.month:02d} ao {end.day:02d}/{end.month:02d}"


def migrate_meta_guild_state(state: dict) -> bool:
    if "phase" in state:
        return False
    previous_total = int(state.pop("current_total_cents", 0))
    previous_members = dict(state.pop("by_member", {}))
    completed = state.setdefault("completed_cycles", [])
    family_wins = [entry for entry in completed if entry.get("reason") == "goal_reached" and int(entry.get("total_cents", 0)) >= META_GOAL_CENTS]
    state.setdefault("submitted_by_member", {})
    state.setdefault("pending_family_thanks", False)
    if family_wins:
        last_win = family_wins[-1]
        state["cycle"] = int(last_win.get("cycle", max(1, int(state.get("cycle", 2)) - 1)))
        state["phase"] = "personal"
        state["family_total_cents"] = META_GOAL_CENTS
        state["family_by_member"] = dict(last_win.get("by_member", {}))
        state["personal_total_cents"] = 0
        state["personal_by_member"] = {}
        state["pending_family_thanks"] = True
    else:
        state["phase"] = "family"
        state["family_total_cents"] = previous_total
        state["family_by_member"] = previous_members
        state["personal_total_cents"] = 0
        state["personal_by_member"] = {}
    return True


def get_meta_guild_state(data: dict, guild_id: int) -> dict:
    guilds = data.setdefault("guilds", {})
    state = guilds.setdefault(str(guild_id), new_meta_guild_state())
    migrate_meta_guild_state(state)
    return state


def parse_brl_cents(value: str) -> int:
    raw = value.strip().lower().replace("r$", "").replace(" ", "")
    if not raw or not re.fullmatch(r"[0-9.,]+", raw):
        raise ValueError("Use um valor positivo, como 1500, 1.500,00 ou 1500.50.")
    if "," in raw:
        if raw.count(",") != 1:
            raise ValueError("Formato de valor inválido.")
        whole, fraction = raw.split(",", 1)
        whole = whole.replace(".", "")
        if len(fraction) > 2:
            raise ValueError("Informe no máximo duas casas decimais.")
        normalized = whole + ("." + fraction if fraction else "")
    else:
        if raw.count(".") > 1:
            normalized = raw.replace(".", "")
        elif "." in raw and len(raw.rsplit(".", 1)[1]) == 3:
            normalized = raw.replace(".", "")
        else:
            normalized = raw
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Formato de valor inválido.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("O valor precisa ser maior que zero.")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("Informe no máximo duas casas decimais.")
    return int(cents)


def format_brl(cents: int) -> str:
    whole, fraction = divmod(max(0, int(cents)), 100)
    grouped = f"{whole:,}".replace(",", ".")
    return f"R$ {grouped},{fraction:02d}"


def meta_embed(guild_id: int) -> discord.Embed:
    data = load_meta_state()
    state = get_meta_guild_state(data, guild_id)
    if sync_meta_week(state):
        save_meta_state(data)
    total = int(state.get("weekly_total_cents", 0))
    site_total = state.get("site_total_cents")
    site_count = int(state.get("site_goal_count", 0))
    site_updated = state.get("site_updated_at")
    site_summary = (
        f"**Acumulado no site:** {format_brl(site_total)} · {site_count} cadastro(s)"
        if site_total is not None
        else "**Acumulado no site:** aguardando sincronização"
    )
    if site_updated:
        try:
            updated_at = datetime.fromisoformat(site_updated).astimezone(SAO_PAULO_TIMEZONE)
            site_summary += f"\nAtualizado em {updated_at:%d/%m às %H:%M}"
        except (TypeError, ValueError):
            pass
    embed = discord.Embed(
        title=f"💰 SEMANA {int(state.get('weekly_number', 1))} · META DE FARME",
        description=(
            f"## {format_brl(total)}\n\n"
            f"Período: **{weekly_period_label(state)} · segunda às 14h → segunda às 14h**\n\n"
            f"{site_summary}"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=META_PANEL_MARKER)
    return embed


def can_reset_meta(member: discord.Member) -> bool:
    role_ids = {
        int(value.strip())
        for value in os.getenv("META_ADMIN_ROLE_IDS", "").split(",")
        if value.strip().isdigit()
    }
    if role_ids:
        return any(role.id in role_ids for role in member.roles)
    role_id = os.getenv("META_ADMIN_ROLE_ID", "").strip()
    if role_id.isdigit():
        return any(role.id == int(role_id) for role in member.roles)
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


async def refresh_meta_panel(guild_id: int) -> None:
    data = load_meta_state()
    state = data.get("guilds", {}).get(str(guild_id), {})
    channel_id = state.get("panel_channel_id")
    message_id = state.get("panel_message_id")
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(int(channel_id)) if guild and channel_id else None
    if not isinstance(channel, discord.TextChannel) or not message_id:
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=meta_embed(guild_id), view=MetaTrackerView())
    except discord.NotFound:
        log.warning("Painel de meta não encontrado para atualizar no servidor %s.", guild_id)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Não foi possível atualizar o painel da meta no servidor %s.", guild_id)


async def publish_meta_panel(guild: discord.Guild, channel: discord.TextChannel) -> discord.Message:
    data = load_meta_state()
    state = get_meta_guild_state(data, guild.id)
    message = None
    if state.get("panel_channel_id") == channel.id and state.get("panel_message_id"):
        try:
            message = await channel.fetch_message(int(state["panel_message_id"]))
        except (discord.NotFound, discord.Forbidden, ValueError):
            message = None
    if message is None:
        async for candidate in channel.history(limit=100):
            if candidate.author == bot.user and any(
                embed.footer and embed.footer.text == META_PANEL_MARKER
                for embed in candidate.embeds
            ):
                message = candidate
                break
    if message:
        await message.edit(content="", embed=meta_embed(guild.id), view=MetaTrackerView())
    else:
        message = await channel.send(embed=meta_embed(guild.id), view=MetaTrackerView())
    state["panel_channel_id"] = channel.id
    state["panel_message_id"] = message.id
    save_meta_state(data)
    await announce_pending_family_thanks(guild)
    return message


async def announce_week_started(guild: discord.Guild, week: dict) -> tuple[int, int]:
    period = week["period"]
    notice = (
        f"📢 **Nova semana da meta de Farme — Semana {week['week_number']}!**\n"
        f"Período: **{period}**, de segunda-feira às 14h até a segunda-feira seguinte às 14h.\n"
        "A contagem semanal foi reiniciada. Registrem as entregas normalmente pelo comando `/registrar-farme` no ticket Farme."
    )
    announcement_channel = find_text_channel(guild, "📍-informativos")
    sent_tickets = 0
    if announcement_channel:
        try:
            me = guild.me
            if me and not announcement_channel.permissions_for(me).mention_everyone:
                overwrite = announcement_channel.overwrites_for(me)
                overwrite.mention_everyone = True
                await announcement_channel.set_permissions(
                    me,
                    overwrite=overwrite,
                    reason="Permitir o aviso de início da semana de Farme solicitado pela administração",
                )
            await announcement_channel.send(
                f"@everyone\n{notice}",
                allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Não foi possível avisar os membros no canal %s.", announcement_channel.name)
    else:
        log.warning("Canal de informativos não encontrado; o aviso geral da Semana %s não foi enviado.", week["week_number"])

    for channel in guild.text_channels:
        if not is_farme_ticket(channel):
            continue
        match = re.search(r"(?:^|; )owner_id=(\d+)(?:;|$)", channel.topic or "")
        owner_mention = f"<@{match.group(1)}> " if match else ""
        try:
            await channel.send(
                f"{owner_mention}📢 **Começou a Semana {week['week_number']} da meta de Farme!**\n"
                f"Período: **{period}**, até segunda-feira às 14h. A contagem desta semana começou do zero.\n"
                "Registre suas entregas neste ticket com `/registrar-farme`.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            sent_tickets += 1
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Não foi possível avisar o dono do ticket Farme %s sobre a nova semana.", channel.id)
    return (1 if announcement_channel else 0), sent_tickets


async def announce_pending_family_thanks(guild: discord.Guild) -> None:
    async with META_LOCK:
        data = load_meta_state()
        state = get_meta_guild_state(data, guild.id)
        pending = bool(state.get("pending_family_thanks"))
        channel_id = state.get("panel_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            if pending:
                log.warning("Meta familiar concluída, mas não há canal do painel para publicar o agradecimento.")
            return
        cycle = int(state.get("cycle", 1))
        footer = f"{META_COMPLETION_FOOTER} · ciclo {cycle}"
        try:
            thank_you_message = None
            async for message in channel.history(limit=100):
                if message.author == bot.user and any(embed.footer and embed.footer.text == footer for embed in message.embeds):
                    thank_you_message = message
                    break
            if thank_you_message:
                # Also repair older completion messages that were sent without their promised buttons.
                await thank_you_message.edit(view=MetaTrackerView())
            elif pending:
                embed = discord.Embed(
                    title="🎉 Meta familiar alcançada!",
                    description=(
                        f"A família completou a meta de **{format_brl(META_GOAL_CENTS)}**! "
                        "Parabéns pelo esforço e pela união de todos — muito obrigado pela contribuição.\n\n"
                        "A etapa pessoal começou do zero. Continuem registrando seus valores pelo botão abaixo."
                    ),
                    color=discord.Color.green(),
                )
                embed.set_footer(text=footer)
                await channel.send(embed=embed, view=MetaTrackerView(), allowed_mentions=discord.AllowedMentions.none())
            elif not pending:
                return
            if not pending:
                return
            state["pending_family_thanks"] = False
            sent_cycles = state.setdefault("family_thanks_sent_cycles", [])
            if cycle not in sent_cycles:
                sent_cycles.append(cycle)
            save_meta_state(data)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Não foi possível publicar o agradecimento da meta familiar concluída.")


async def reconcile_meta_state(guild: discord.Guild) -> None:
    async with META_LOCK:
        data = load_meta_state()
        get_meta_guild_state(data, guild.id)
        save_meta_state(data)
    await announce_pending_family_thanks(guild)
    await refresh_meta_panel(guild.id)


def apply_meta_amount(state: dict, member_id: int, amount_cents: int) -> dict:
    """Apply the amount to the current family or personal stage."""
    member_key = str(member_id)
    if state.get("phase", "family") == "family":
        current = int(state.get("family_total_cents", 0))
        remaining = META_GOAL_CENTS - current
        if amount_cents > remaining:
            raise ValueError(f"A meta familiar está em {format_brl(current)}. Registre no máximo os {format_brl(remaining)} que faltam; a etapa pessoal começará do zero após concluir a meta familiar.")
        sync_meta_week(state)
        state["weekly_total_cents"] = int(state.get("weekly_total_cents", 0)) + amount_cents
        state.setdefault("submitted_by_member", {})[member_key] = int(state.setdefault("submitted_by_member", {}).get(member_key, 0)) + amount_cents
        state.setdefault("family_by_member", {})[member_key] = int(state.setdefault("family_by_member", {}).get(member_key, 0)) + amount_cents
        state["family_total_cents"] = current + amount_cents
        if state["family_total_cents"] == META_GOAL_CENTS:
            state.setdefault("completed_cycles", []).append({
                "cycle": int(state.get("cycle", 1)),
                "total_cents": META_GOAL_CENTS,
                "by_member": dict(state.get("family_by_member", {})),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "reason": "goal_reached",
            })
            state["phase"] = "personal"
            state["personal_total_cents"] = 0
            state["personal_by_member"] = {}
            state["pending_family_thanks"] = True
            return {"family_completed": True, "cycle": int(state.get("cycle", 1)), "total_cents": 0, "phase": "personal"}
        return {"family_completed": False, "cycle": int(state.get("cycle", 1)), "total_cents": int(state["family_total_cents"]), "phase": "family"}
    sync_meta_week(state)
    state["weekly_total_cents"] = int(state.get("weekly_total_cents", 0)) + amount_cents
    state.setdefault("submitted_by_member", {})[member_key] = int(state.setdefault("submitted_by_member", {}).get(member_key, 0)) + amount_cents
    state.setdefault("personal_by_member", {})[member_key] = int(state.setdefault("personal_by_member", {}).get(member_key, 0)) + amount_cents
    state["personal_total_cents"] = int(state.get("personal_total_cents", 0)) + amount_cents
    return {
        "family_completed": False,
        "cycle": int(state.get("cycle", 1)),
        "total_cents": int(state["personal_total_cents"]),
        "member_total_cents": int(state["personal_by_member"][member_key]),
        "phase": "personal",
    }


async def reset_meta_cycle(guild_id: int, moderator_id: int) -> dict:
    async with META_LOCK:
        data = load_meta_state()
        state = get_meta_guild_state(data, guild_id)
        old_cycle = int(state.get("cycle", 1))
        old_total = int(state.get("current_total_cents", 0))
        state.setdefault("completed_cycles", []).append({
            "cycle": old_cycle,
            "phase": state.get("phase", "family"),
            "family_total_cents": int(state.get("family_total_cents", 0)),
            "family_by_member": dict(state.get("family_by_member", {})),
            "personal_total_cents": int(state.get("personal_total_cents", 0)),
            "personal_by_member": dict(state.get("personal_by_member", {})),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "reason": "admin_reset",
            "reset_by": str(moderator_id),
        })
        state["cycle"] = old_cycle + 1
        state["phase"] = "family"
        state["family_total_cents"] = 0
        state["family_by_member"] = {}
        state["personal_total_cents"] = 0
        state["personal_by_member"] = {}
        state["submitted_by_member"] = {}
        sync_meta_week(state)
        state["weekly_total_cents"] = 0
        state["pending_family_thanks"] = False
        save_meta_state(data)
        return state


async def close_meta_week(guild_id: int, moderator_id: int) -> dict:
    async with META_LOCK:
        data = load_meta_state()
        state = get_meta_guild_state(data, guild_id)
        sync_meta_week(state)
        start = datetime.fromisoformat(state["weekly_start_at"])
        end = datetime.fromisoformat(state["weekly_end_at"])
        now = datetime.now(SAO_PAULO_TIMEZONE)
        if now < end:
            raise ValueError(f"A Semana {state['weekly_number']} só pode ser encerrada a partir de segunda-feira, {end:%d/%m}, às 14h.")

        old_number = int(state.get("weekly_number", 1))
        old_total = int(state.get("weekly_total_cents", 0))
        period = format_week_dates(start, end)
        state.setdefault("weekly_history", []).append({
            "week_number": old_number,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "period": period,
            "total_cents": old_total,
            "closed_at": now.isoformat(),
            "closed_by": str(moderator_id),
        })

        next_start = end
        next_number = old_number + 1
        while now >= next_start + timedelta(days=7):
            next_start += timedelta(days=7)
            next_number += 1
        next_end = next_start + timedelta(days=7)
        state["weekly_number"] = next_number
        state["weekly_start_at"] = next_start.isoformat()
        state["weekly_end_at"] = next_end.isoformat()
        state["weekly_key"] = next_start.date().isoformat()
        state["weekly_total_cents"] = 0
        save_meta_state(data)
        return {
            "closed_week": old_number,
            "closed_period": period,
            "closed_total_cents": old_total,
            "week_number": next_number,
            "start_at": next_start.isoformat(),
            "end_at": next_end.isoformat(),
            "period": format_week_dates(next_start, next_end),
        }


def persist_discovered_ids(guild: discord.Guild) -> None:
    updates: dict[str, str] = {}
    if len(bot.guilds) == 1:
        updates["DISCORD_GUILD_ID"] = str(guild.id)
    channel_specs = {
        PANEL_NAME: "PANEL_CHANNEL_ID",
        ABSENCE_NAME: "ABSENCE_CHANNEL_ID",
        IDENTIFICATION_NAME: "IDENTIFICATION_CHANNEL_ID",
    }
    for name, key in channel_specs.items():
        matches = [c for c in guild.text_channels if c.name == name]
        if not matches:
            matches = [c for c in guild.text_channels if norm(c.name) == norm(name)]
        if len(matches) == 1:
            updates[key] = str(matches[0].id)
            log.info("Canal identificado: %s (ID %s).", name, matches[0].id)
        else:
            if key == "ABSENCE_CHANNEL_ID":
                nearby = [c.name for c in guild.text_channels if any(term in norm(c.name) for term in ("ausen", "ausenc", "absence"))]
            elif key == "IDENTIFICATION_CHANNEL_ID":
                nearby = [c.name for c in guild.text_channels if any(term in norm(c.name) for term in ("identificacao", "identification"))]
            else:
                nearby = [c.name for c in guild.text_channels if "painel" in norm(c.name) or "chamado" in norm(c.name)]
            log.warning("Canal %s: encontrados %d; opções semelhantes: %s.", name, len(matches), ", ".join(nearby) or "nenhuma")
    categories = [c for c in guild.categories if "farme" in norm(c.name)]
    if len(categories) == 1:
        updates["FARME_CATEGORY_ID"] = str(categories[0].id)
        log.info("Categoria Farme identificada: %s (ID %s).", categories[0].name, categories[0].id)
    else:
        log.warning("Categorias semelhantes a Farme: %s.", ", ".join(f"{c.name} ({c.id})" for c in categories) or "nenhuma")
    role_specs = {
        "🚀 LÍDER": "ROLE_LEADER_ID",
        "🔥 SUB LÍDER": "ROLE_SUB_LEADER_ID",
        "[RESP] FARME": "ROLE_FARME_RESP_ID",
    }
    for name, key in role_specs.items():
        matches = [r for r in guild.roles if r.name == name]
        if not matches:
            matches = [r for r in guild.roles if norm(r.name) == norm(name)]
        if len(matches) == 1:
            updates[key] = str(matches[0].id)
            log.info("Cargo identificado: %s (ID %s).", name, matches[0].id)
        else:
            hint = norm(name).replace("resp", "")
            nearby = [r for r in guild.roles if hint and (hint in norm(r.name) or norm(r.name) in hint)]
            options = "; ".join(f"{r.name} (ID {r.id}, posição {r.position})" for r in nearby) or "nenhuma"
            log.warning("Cargo %s: encontrados %d; opções semelhantes: %s.", name, len(matches), options)
    if not os.getenv("META_ADMIN_ROLE_ID", "").strip():
        admin_roles = [r for r in guild.roles if norm(r.name) in {"admin", "adm", "administrador"}]
        if len(admin_roles) == 1:
            updates["META_ADMIN_ROLE_ID"] = str(admin_roles[0].id)
            log.info("Cargo administrador identificado para o reset da meta: %s (ID %s).", admin_roles[0].name, admin_roles[0].id)
        elif len(admin_roles) > 1:
            log.warning("Há cargos administradores ambíguos para a meta: %s.", "; ".join(f"{r.name} (ID {r.id})" for r in admin_roles))
    if not updates:
        return
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    seen: set[str] = set()
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
        os.environ[key] = value
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")


def find_text_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    id_key = {
        PANEL_NAME: "PANEL_CHANNEL_ID",
        ABSENCE_NAME: "ABSENCE_CHANNEL_ID",
        IDENTIFICATION_NAME: "IDENTIFICATION_CHANNEL_ID",
    }.get(name)
    configured = os.getenv(id_key, "").strip() if id_key else ""
    if configured.isdigit():
        return discord.utils.get(guild.text_channels, id=int(configured))
    exact = [c for c in guild.text_channels if c.name == name]
    if not exact:
        exact = [c for c in guild.text_channels if norm(c.name) == norm(name)]
    return exact[0] if len(exact) == 1 else None


def find_meta_panel_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if META_PANEL_CHANNEL_ID.isdigit():
        channel = guild.get_channel(int(META_PANEL_CHANNEL_ID))
        return channel if isinstance(channel, discord.TextChannel) else None
    return find_text_channel(guild, "『🕹️』ᴄᴏɴᴛᴏʟᴇ-ᴅᴇ-ꜰᴀʀᴍᴇ")


def find_farme_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    configured = os.getenv("FARME_CATEGORY_ID", "").strip()
    if configured.isdigit():
        found = discord.utils.get(guild.categories, id=int(configured))
        if found:
            return found
    candidates = [c for c in guild.categories if "farme" in norm(c.name)]
    return candidates[0] if len(candidates) == 1 else None


def resolve_farme_roles(guild: discord.Guild) -> tuple[list[discord.Role], list[str]]:
    role_keys = {"🚀 LÍDER": "ROLE_LEADER_ID", "🔥 SUB LÍDER": "ROLE_SUB_LEADER_ID", "[RESP] FARME": "ROLE_FARME_RESP_ID"}
    selected: list[discord.Role] = []
    missing: list[str] = []
    for name in FARME_ROLE_NAMES:
        configured = os.getenv(role_keys[name], "").strip()
        role = discord.utils.get(guild.roles, id=int(configured)) if configured.isdigit() else None
        matches = [role] if role else []
        if not configured:
            matches = [r for r in guild.roles if r.name == name]
            if not matches:
                matches = [r for r in guild.roles if norm(r.name) == norm(name)]
        if not matches:
            missing.append(name)
        else:
            selected.extend(matches)
    selected = list({role.id: role for role in selected}.values())
    return selected, missing


def farme_role_overwrites(guild: discord.Guild, member: discord.Member, roles: list[discord.Role] | None = None) -> dict:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
    }
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
    selected_roles = roles if roles is not None else resolve_farme_roles(guild)[0]
    for role in selected_roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    return overwrites


async def reconcile_farme_ticket_permissions(guild: discord.Guild) -> int:
    """Ensure the ticket owner and authorized farm roles can read the full history."""
    roles, _missing = resolve_farme_roles(guild)
    updated = 0
    for channel in guild.text_channels:
        if not is_farme_ticket(channel):
            continue
        match = re.search(r"(?:^|; )owner_id=(\d+)(?:;|$)", channel.topic or "")
        if not match:
            continue
        owner_id = int(match.group(1))
        owner = guild.get_member(owner_id)

        targets = [*roles]
        # The owner overwrite is created with every new ticket. Only touch it
        # when the member is cached; discord.py requires a Member/User object.
        if owner is not None:
            targets.append(owner)
        changed = False
        try:
            # Keep the channel private while granting explicit access to its staff.
            everyone = channel.overwrites_for(guild.default_role)
            if everyone.view_channel is not False:
                everyone.view_channel = False
                await channel.set_permissions(guild.default_role, overwrite=everyone, reason="Manter ticket Farme privado")
                changed = True
            for target in targets:
                overwrite = channel.overwrites_for(target)
                needed = {
                    "view_channel": True,
                    "send_messages": True,
                    "read_message_history": True,
                    "attach_files": True,
                    "embed_links": True,
                }
                if any(getattr(overwrite, permission) is not True for permission in needed):
                    for permission, value in needed.items():
                        setattr(overwrite, permission, value)
                    await channel.set_permissions(target, overwrite=overwrite, reason="Acesso do dono/equipe autorizada ao ticket Farme")
                    changed = True
            if changed:
                updated += 1
        except discord.Forbidden:
            log.warning("Sem permissão para ajustar os acessos do ticket Farme %s.", channel.id)
        except discord.HTTPException:
            log.exception("Falha ao ajustar os acessos do ticket Farme %s.", channel.id)
    return updated


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="stryke:ticket:close:v1")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Este canal não pode ser fechado por aqui.", ephemeral=True)
        await interaction.response.send_message("Confirma a exclusão deste canal de ticket?", view=ConfirmDeleteView(), ephemeral=True)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirmar exclusão", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not channel.name.startswith("farme-"):
            return await interaction.response.send_message("Só canais de ticket Farme podem ser excluídos por este botão.", ephemeral=True)
        await interaction.response.send_message("Ticket encerrado.", ephemeral=True)
        try:
            await channel.delete(reason=f"Ticket fechado por {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            log.error("Sem permissão para excluir ticket %s.", channel.id)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Exclusão cancelada.", ephemeral=True)


class AbsenceModal(discord.ui.Modal, title="Registrar ausência"):
    nome = discord.ui.TextInput(label="Nome", max_length=100)
    dias = discord.ui.TextInput(label="Quantidade de dias", max_length=4, placeholder="Ex.: 3")
    inicio = discord.ui.TextInput(label="Data de início", max_length=40, placeholder="DD/MM/AAAA")
    motivo = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            return await interaction.response.send_message("Use este formulário dentro do servidor.", ephemeral=True)
        try:
            days = int(str(self.dias.value).strip())
            if not 1 <= days <= 3650:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Informe um número de dias entre 1 e 3650 e tente novamente.", ephemeral=True)
        destination = find_text_channel(guild, ABSENCE_NAME)
        if destination is None:
            return await interaction.response.send_message(f"Não encontrei o canal `{ABSENCE_NAME}` com nome exato. Avise um administrador.", ephemeral=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
        channel = None
        try:
            channel = await guild.create_text_channel(
                f"ausencia-{safe_slug(member.display_name)}", overwrites=overwrites,
                topic=f"Canal temporário de ausência de {member} ({member.id}); criado pelo STRYKE BOT.",
                reason=f"Registro de ausência iniciado por {member} ({member.id})",
            )
            embed = discord.Embed(title="😇 Registro de ausência", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Membro", value=member.mention, inline=False)
            embed.add_field(name="Nome informado", value=str(self.nome.value).strip(), inline=True)
            embed.add_field(name="Dias", value=str(days), inline=True)
            embed.add_field(name="Início", value=str(self.inicio.value).strip(), inline=True)
            embed.add_field(name="Motivo", value=str(self.motivo.value).strip(), inline=False)
            embed.set_footer(text=f"ID do membro: {member.id}")
            await destination.send(content=member.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            await channel.send(f"{member.mention}, seu registro foi enviado para {destination.mention}. Este canal privado será excluído automaticamente em 15 minutos.", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            await interaction.response.send_message(f"Ausência registrada em {destination.mention}. Seu canal privado temporário é {channel.mention}.", ephemeral=True)
            asyncio.create_task(delete_temporary_channel(channel))
        except discord.Forbidden:
            log.exception("Permissões insuficientes ao processar ausência.")
            if channel:
                try:
                    await channel.delete(reason="Limpeza após falha no registro de ausência")
                except discord.HTTPException:
                    pass
            if not interaction.response.is_done():
                await interaction.response.send_message("Não tenho permissões suficientes para concluir o registro. Avise um administrador.", ephemeral=True)
        except discord.HTTPException:
            log.exception("Erro do Discord ao processar ausência.")
            if not interaction.response.is_done():
                await interaction.response.send_message("O Discord não aceitou o registro agora. Tente novamente mais tarde.", ephemeral=True)


async def delete_temporary_channel(channel: discord.TextChannel, delay: float = 15 * 60):
    await asyncio.sleep(delay)
    try:
        await channel.delete(reason="Canal temporário de registro de ausência (15 minutos)")
    except discord.NotFound:
        pass
    except discord.Forbidden:
        log.error("Sem permissão para remover o canal temporário de ausência %s.", channel.id)
    except discord.HTTPException:
        log.exception("Falha ao remover canal temporário de ausência %s.", channel.id)


class MetaContributionModal(discord.ui.Modal, title="Registrar valor para a meta"):
    amount = discord.ui.TextInput(
        label="Valor para registrar (R$)",
        placeholder="Ex.: 1500, 1.500,00 ou 1500.50",
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use este formulário dentro do servidor.", ephemeral=True)
        try:
            amount_cents = parse_brl_cents(str(self.amount.value))
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with META_LOCK:
            data = load_meta_state()
            state = get_meta_guild_state(data, interaction.guild.id)
            try:
                result = apply_meta_amount(state, interaction.user.id, amount_cents)
            except ValueError as exc:
                return await interaction.followup.send(str(exc), ephemeral=True)
            save_meta_state(data)
        if result["family_completed"]:
            await announce_pending_family_thanks(interaction.guild)
        await refresh_meta_panel(interaction.guild.id)
        if result["family_completed"]:
            await interaction.followup.send(
                f"Valor de {format_brl(amount_cents)} registrado. A meta familiar de {format_brl(META_GOAL_CENTS)} foi concluída! Obrigado pelo esforço de todos. A meta pessoal começou do zero.",
                ephemeral=True,
            )
        elif result["phase"] == "personal":
            await interaction.followup.send(
                f"Valor de {format_brl(amount_cents)} registrado. Seu total pessoal nesta etapa é {format_brl(result['member_total_cents'])}; essa confirmação é visível somente para você. O painel mostra apenas o total geral.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Valor de {format_brl(amount_cents)} registrado. Meta familiar: {format_brl(result['total_cents'])} de {format_brl(META_GOAL_CENTS)}.",
                ephemeral=True,
            )


class IdentificationModal(discord.ui.Modal, title="Cadastro de identificação"):
    nome = discord.ui.TextInput(label="Nome", max_length=32)
    vulgo = discord.ui.TextInput(label="Vulgo", max_length=100)
    passaporte = discord.ui.TextInput(label="Passaporte", max_length=32)
    cell = discord.ui.TextInput(label="Cell (in game)", max_length=100)
    recrutador = discord.ui.TextInput(label="Recrutador", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use este formulário dentro do servidor.", ephemeral=True)
        values = {
            "Nome": str(self.nome.value).strip(),
            "Vulgo": str(self.vulgo.value).strip(),
            "Passaporte": str(self.passaporte.value).strip(),
            "Cell (in game)": str(self.cell.value).strip(),
            "Recrutador": str(self.recrutador.value).strip(),
        }
        empty = [label for label, value in values.items() if not value]
        if empty:
            return await interaction.response.send_message(
                "Preencha todos os campos: " + ", ".join(empty) + ".",
                ephemeral=True,
            )
        destination = find_identification_storage_channel(guild)
        if destination is None:
            return await interaction.response.send_message(
                "O canal privado de identificações ainda não está pronto. Avise um administrador.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with IDENTIFICATION_LOCK:
            state = load_state()
            guild_state = state.setdefault(str(guild.id), {})
            number = int(guild_state.get("identification_last_number", 0)) + 1
            # Reserve the number before posting so a restart cannot reuse it.
            guild_state["identification_last_number"] = number
            save_state(state)
            embed = discord.Embed(
                title=f"🪪 Cadastro de identificação #{number:04d}",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            for label, value in values.items():
                embed.add_field(name=label, value=value, inline=(label != "Cell (in game)"))
            embed.add_field(name="Status", value="⏳ Aguardando análise", inline=False)
            embed.set_footer(text=f"Enviado por {interaction.user.display_name} · ID {interaction.user.id} · STRYKE ID {guild.id}:{number}")
            try:
                await destination.send(
                    embed=embed,
                    view=IdentificationReviewView(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                return await interaction.followup.send(
                    f"Não consigo publicar o cadastro #{number:04d}. Avise um administrador para conferir minhas permissões.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                log.exception("Falha ao publicar cadastro de identificação no canal %s.", destination.id)
                return await interaction.followup.send(
                    f"O Discord não conseguiu publicar o cadastro #{number:04d}. Tente novamente.",
                    ephemeral=True,
                )
        await interaction.followup.send(
            f"Cadastro #{number:04d} enviado para análise. Você receberá a aprovação da equipe.",
            ephemeral=True,
        )


class IdentificationReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _reviewer(self, interaction: discord.Interaction) -> discord.Member | None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Esta ação só pode ser usada dentro do servidor.", ephemeral=True)
            return None
        if not can_review_identification(interaction.user):
            await interaction.response.send_message("Somente o cargo de identificação ou a gerência pode decidir este cadastro.", ephemeral=True)
            return None
        if not isinstance(interaction.message, discord.Message) or not interaction.message.embeds:
            await interaction.response.send_message("Não consegui localizar os dados deste cadastro.", ephemeral=True)
            return None
        status = next((field.value for field in interaction.message.embeds[0].fields if field.name == "Status"), "⏳ Aguardando análise")
        if status != "⏳ Aguardando análise":
            await interaction.response.send_message(f"Este cadastro já foi decidido: {status}.", ephemeral=True)
            return None
        return interaction.user

    def _marked_embed(self, original: discord.Embed, status: str, reviewer: discord.Member) -> discord.Embed:
        embed = original.copy()
        for index, field in enumerate(embed.fields):
            if field.name == "Status":
                embed.set_field_at(index, name="Status", value=status, inline=False)
                break
        else:
            embed.add_field(name="Status", value=status, inline=False)
        embed.add_field(name="Decidido por", value=reviewer.mention, inline=False)
        embed.color = discord.Color.green() if status.startswith("✅") else discord.Color.red()
        return embed

    @discord.ui.button(label="Aceitar", emoji="✅", style=discord.ButtonStyle.success, custom_id="stryke:identification:approve:v1")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        reviewer = await self._reviewer(interaction)
        if reviewer is None:
            return
        embed = interaction.message.embeds[0]
        footer = embed.footer.text if embed.footer else ""
        match = re.search(r"ID (\d+) · STRYKE ID (\d+):(\d+)$", footer)
        fields = {field.name: field.value.strip() for field in embed.fields}
        name = fields.get("Nome", "")
        passport = fields.get("Passaporte", "")
        if not match or not name or not passport:
            return await interaction.response.send_message("O cadastro não contém o usuário, passaporte ou nome esperado; não apliquei alterações.", ephemeral=True)
        nickname = identification_nickname(passport, name)
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        try:
            member = guild.get_member(int(match.group(1))) or await guild.fetch_member(int(match.group(1)))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await interaction.followup.send("Não localizei o membro no servidor; o cadastro continua pendente.", ephemeral=True)
        bot_member = guild.me
        roles = [role for role in guild.roles if norm(role.name) == norm("Membro") and not role.managed and bot_member and role < bot_member.top_role]
        if not roles:
            return await interaction.followup.send("Não encontrei um cargo `Membro` que o bot possa atribuir; o cadastro continua pendente.", ephemeral=True)
        member_role = max(roles, key=lambda role: role.position)
        try:
            if member_role not in member.roles:
                await member.add_roles(member_role, reason=f"Identificação STRYKE #{match.group(3)} aprovada por {reviewer}")
            await member.edit(nick=nickname, reason=f"Identificação STRYKE #{match.group(3)} aprovada por {reviewer}")
        except discord.Forbidden:
            return await interaction.followup.send("O Discord negou a atribuição do cargo ou alteração do apelido. Confira as permissões e a hierarquia do bot; o cadastro continua pendente.", ephemeral=True)
        except discord.HTTPException:
            log.exception("Falha ao aplicar cadastro STRYKE #%s após aprovação.", match.group(3))
            return await interaction.followup.send("O Discord não conseguiu concluir as alterações; o cadastro continua pendente.", ephemeral=True)
        button.disabled = True
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=self._marked_embed(embed, "✅ Aprovada", reviewer), view=self)
        await interaction.followup.send(f"Cadastro aprovado. Cargo `Membro` e apelido **{nickname}** aplicados a {member.mention}.", ephemeral=True)

    @discord.ui.button(label="Recusar", emoji="❌", style=discord.ButtonStyle.danger, custom_id="stryke:identification:reject:v1")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        reviewer = await self._reviewer(interaction)
        if reviewer is None:
            return
        button.disabled = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=self._marked_embed(interaction.message.embeds[0], "❌ Recusada", reviewer), view=self)


class FarmeRecordModal(discord.ui.Modal, title="Registrar farm no ticket"):
    entregou_para = discord.ui.TextInput(label="Entregou para quem?", max_length=100)
    valor = discord.ui.TextInput(label="Valor do farm (R$)", placeholder="Ex.: 100000", max_length=30)
    bau = discord.ui.TextInput(label="O que colocou no baú?", style=discord.TextStyle.paragraph, max_length=1000)
    observacao = discord.ui.TextInput(label="Observação (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.channel
        member = interaction.user
        if not interaction.guild or not isinstance(channel, discord.TextChannel) or not isinstance(member, discord.Member):
            return await interaction.response.send_message("Use este comando dentro do seu ticket Farme.", ephemeral=True)
        if not is_farme_ticket(channel):
            return await interaction.response.send_message("Use este formulário dentro de um ticket Farme criado pelo bot.", ephemeral=True)
        try:
            value_cents = parse_brl_cents(str(self.valor.value))
        except ValueError as exc:
            return await interaction.response.send_message(f"Valor inválido: {exc}", ephemeral=True)
        embed = discord.Embed(title="📦 Registro de farm", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Membro", value=member.mention, inline=True)
        embed.add_field(name="Entregou para", value=str(self.entregou_para.value).strip(), inline=True)
        embed.add_field(name="Valor do farm", value=format_brl(value_cents), inline=True)
        embed.add_field(name="Itens no baú", value=str(self.bau.value).strip(), inline=False)
        if str(self.observacao.value).strip():
            embed.add_field(name="Observação", value=str(self.observacao.value).strip(), inline=False)
        embed.set_footer(text=f"Registrado por {member} · ID {member.id}")
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            return await interaction.response.send_message("Não consigo publicar o registro neste ticket. Avise um administrador.", ephemeral=True)
        except discord.HTTPException:
            log.exception("Falha ao publicar registro de farm no canal %s.", channel.id)
            return await interaction.response.send_message("O Discord não conseguiu publicar o registro agora. Tente novamente.", ephemeral=True)
        await interaction.response.send_message("Farm registrado no ticket. Agora envie os prints obrigatórios como anexos neste canal.", ephemeral=True)


class MetaResetConfirmView(discord.ui.View):
    def __init__(self, actor_id: int):
        super().__init__(timeout=60)
        self.actor_id = actor_id

    @discord.ui.button(label="Confirmar reset", style=discord.ButtonStyle.danger)
    async def confirm_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Somente quem iniciou esta confirmação pode usá-la.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not can_reset_meta(interaction.user):
            return await interaction.response.send_message("Somente o cargo administrador configurado pode resetar a meta.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("Este botão só funciona no servidor.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = await reset_meta_cycle(interaction.guild.id, interaction.user.id)
        await refresh_meta_panel(interaction.guild.id)
        await interaction.followup.send(
            f"Tudo foi resetado. O ciclo familiar #{state['cycle']} começou do zero, com uma nova meta de R$ 100.000,00. O histórico anterior foi preservado.",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Somente quem iniciou esta confirmação pode usá-la.", ephemeral=True)
        await interaction.response.send_message("Reset cancelado; a contagem continua igual.", ephemeral=True)


class MetaTrackerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Registrar valor", emoji="💰", style=discord.ButtonStyle.primary, custom_id="stryke:meta:add:v1")
    async def add_contribution(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MetaContributionModal())

    @discord.ui.button(label="Resetar", emoji="🔄", style=discord.ButtonStyle.danger, custom_id="stryke:meta:reset:v1")
    async def reset_cycle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_reset_meta(interaction.user):
            return await interaction.response.send_message("Somente o cargo administrador configurado pode resetar a meta.", ephemeral=True)
        await interaction.response.send_message(
            "Confirma zerar toda a contagem atual e iniciar do zero uma nova meta familiar de R$ 100.000,00? O histórico será preservado.",
            view=MetaResetConfirmView(interaction.user.id),
            ephemeral=True,
        )


class WeeklyMetaCloseConfirmView(discord.ui.View):
    def __init__(self, actor_id: int):
        super().__init__(timeout=90)
        self.actor_id = actor_id

    @discord.ui.button(label="Confirmar fechamento", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Somente quem iniciou esta confirmação pode usá-la.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not can_reset_meta(interaction.user):
            return await interaction.response.send_message("Somente os cargos autorizados podem fechar a semana.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("Este botão só funciona no servidor.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            week = await close_meta_week(interaction.guild.id, interaction.user.id)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        channel = find_meta_panel_channel(interaction.guild)
        try:
            if channel:
                await publish_meta_panel(interaction.guild, channel)
            else:
                await refresh_meta_panel(interaction.guild.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("A Semana %s foi fechada, mas o painel não foi atualizado.", week["closed_week"])
        announcement_count, ticket_count = await announce_week_started(interaction.guild, week)
        await interaction.followup.send(
            f"Semana {week['closed_week']} ({week['closed_period']}) encerrada com {format_brl(week['closed_total_cents'])}. "
            f"A Semana {week['week_number']} ({week['period']}) começou do zero. Aviso geral enviado: {'sim' if announcement_count else 'não'}; "
            f"tickets Farme avisados: {ticket_count}.",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Somente quem iniciou esta confirmação pode usá-la.", ephemeral=True)
        await interaction.response.send_message("Fechamento cancelado; a semana continua aberta.", ephemeral=True)


class WeeklyMetaControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Atualizar painel",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        custom_id="stryke:weekly-meta:publish:v1",
    )
    async def update_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_reset_meta(interaction.user):
            return await interaction.response.send_message("Somente os cargos autorizados podem atualizar o painel.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("Este botão só funciona no servidor.", ephemeral=True)
        channel = find_meta_panel_channel(interaction.guild)
        if channel is None:
            return await interaction.response.send_message("Não encontrei o canal Controle de Farme configurado.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot = await fetch_site_goals()
            store_site_goal_snapshot(interaction.guild.id, snapshot)
            message = await publish_meta_panel(interaction.guild, channel)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            log.exception("Falha ao buscar dados do site pelo botão de atualizar painel.")
            return await interaction.followup.send(
                "Não consegui buscar os dados atuais do site. O painel não foi atualizado; tente novamente em instantes.",
                ephemeral=True,
            )
        except discord.Forbidden:
            return await interaction.followup.send("Não tenho permissão para atualizar o painel nesse canal.", ephemeral=True)
        except discord.HTTPException:
            log.exception("Falha ao atualizar o painel semanal pelo botão.")
            return await interaction.followup.send("O Discord não conseguiu atualizar o painel agora.", ephemeral=True)
        await interaction.followup.send(f"Painel semanal atualizado em {message.channel.mention}.", ephemeral=True)

    @discord.ui.button(
        label="Fechar meta e abrir semana",
        emoji="⏭️",
        style=discord.ButtonStyle.success,
        custom_id="stryke:weekly-meta:close:v1",
    )
    async def close_week(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_reset_meta(interaction.user):
            return await interaction.response.send_message("Somente os cargos autorizados podem fechar a meta semanal.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("Este botão só funciona no servidor.", ephemeral=True)
        data = load_meta_state()
        state = get_meta_guild_state(data, interaction.guild.id)
        if sync_meta_week(state):
            save_meta_state(data)
        await interaction.response.send_message(
            f"Confirma encerrar a Semana {state['weekly_number']} ({weekly_period_label(state)}), arquivar {format_brl(state['weekly_total_cents'])} e abrir a próxima? "
            "A confirmação avisa todos em #informativos e publica um recado nos tickets Farme abertos.",
            view=WeeklyMetaCloseConfirmView(interaction.user.id),
            ephemeral=True,
        )


class IdentificationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Preencher identificação",
        emoji="📝",
        style=discord.ButtonStyle.primary,
        custom_id="stryke:identification:open:v1",
    )
    async def open_identification(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use este botão dentro do servidor.", ephemeral=True)
        await interaction.response.send_modal(IdentificationModal())


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir Farme", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="stryke:panel:farme:v1")
    async def open_farme(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
        member = interaction.user
        existing = next((c for c in guild.text_channels if c.name == f"farme-{safe_slug(member.display_name)}" and c.topic and f"owner_id={member.id}" in c.topic), None)
        if existing:
            return await interaction.response.send_message(f"Você já tem um ticket aberto: {existing.mention}", ephemeral=True)
        category = find_farme_category(guild)
        if not category:
            options = [c.name for c in guild.categories if "farm" in norm(c.name)]
            detail = ", ".join(options) if options else "nenhuma categoria semelhante foi encontrada"
            log.warning("Categoria Farme ausente ou ambígua em %s: %s", guild.id, detail)
            return await interaction.response.send_message(f"Não consegui identificar uma única categoria de Farme ({detail}). Um administrador precisa configurar FARME_CATEGORY_ID no .env.", ephemeral=True)
        farme_roles, missing_roles = resolve_farme_roles(guild)
        if missing_roles:
            log.warning("Ticket aberto sem alguns cargos de Farme correspondentes (%s); a solicitação do membro não será bloqueada.", ", ".join(missing_roles))
        meta_data = load_meta_state()
        weekly_state = get_meta_guild_state(meta_data, guild.id)
        if sync_meta_week(weekly_state):
            save_meta_state(meta_data)
        weekly_welcome = (
            f"📅 **Semana {weekly_state['weekly_number']} · {weekly_period_label(weekly_state)}** "
            "(segunda-feira às 14h até a próxima segunda às 14h).\n\n"
        )
        try:
            channel = await guild.create_text_channel(
                f"farme-{safe_slug(member.display_name)}", category=category,
                overwrites=farme_role_overwrites(guild, member, farme_roles),
                topic=f"STRYKE Farme ticket; owner_id={member.id}; criado={datetime.now(timezone.utc).isoformat()}",
                reason=f"Ticket de Farme aberto por {member} ({member.id})",
            )
            await channel.send(
                f"🎫 **Farme Aberto**\n\n🎫 Olá, {member.mention}! 👋\n\n{weekly_welcome}Para registrar, use `/registrar-farme` e informe para quem entregou, o valor e o que colocou no baú 📦. Depois envie os prints obrigatórios como anexos aqui no ticket 📸.\n\nMeta: **100k sujo**. A equipe responsável pelo Farme também pode acompanhar este canal.",
                view=CloseTicketView(), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            await interaction.response.send_message(f"Seu ticket foi criado: {channel.mention}", ephemeral=True)
        except discord.Forbidden:
            log.exception("Permissões insuficientes para criar ticket Farme.")
            await interaction.response.send_message("Não tenho permissões para criar o ticket. Verifique Gerenciar canais e a hierarquia dos cargos.", ephemeral=True)
        except discord.HTTPException:
            log.exception("Falha ao criar ticket Farme.")
            await interaction.response.send_message("O Discord não conseguiu criar o ticket. Tente novamente.", ephemeral=True)

    @discord.ui.button(label="Registrar ausência", emoji="😇", style=discord.ButtonStyle.success, custom_id="stryke:panel:absence:v1")
    async def open_absence(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AbsenceModal())


bot = StrykeBot()


@bot.tree.command(name="painel", description="Publica ou atualiza o painel permanente do STRYKE BOT.")
@app_commands.guild_only()
async def painel(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Este comando só funciona no servidor.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    success = await bot.ensure_panel(interaction.guild)
    channel = find_text_channel(interaction.guild, PANEL_NAME)
    if channel is None:
        return await interaction.followup.send(f"Canal `{PANEL_NAME}` não encontrado.", ephemeral=True)
    if success:
        await interaction.followup.send(f"Painel publicado ou atualizado em {channel.mention}.", ephemeral=True)
    else:
        await interaction.followup.send("Não consegui publicar ou atualizar o painel. Confira os logs e as permissões do bot.", ephemeral=True)


async def clear_ticket_messages(channel, requester: discord.abc.User) -> int:
    messages = [message async for message in channel.history(limit=None, oldest_first=True)]
    if len(messages) <= 1:
        return 0
    deleted = await channel.purge(
        limit=None,
        after=messages[0],
        bulk=False,
        reason=f"Limpeza do ticket solicitada por {requester} ({requester.id})",
    )
    return len(deleted)


@bot.tree.command(name="clear", description="Limpa as mensagens do ticket e mantém a mensagem inicial.")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    if not guild or not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Use este comando dentro de um ticket de texto.", ephemeral=True)
    if not channel.name.startswith("farme-") or not (channel.topic or "").startswith("STRYKE Farme ticket;"):
        return await interaction.response.send_message("O `/clear` só pode ser usado nos tickets Farme criados pelo bot.", ephemeral=True)
    bot_member = guild.me
    if not bot_member or not channel.permissions_for(bot_member).manage_messages or not channel.permissions_for(bot_member).read_message_history:
        return await interaction.response.send_message("Preciso das permissões **Gerenciar mensagens** e **Ver histórico de mensagens** neste ticket.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        deleted_count = await clear_ticket_messages(channel, interaction.user)
        if deleted_count == 0:
            return await interaction.followup.send("O ticket já está limpo; a mensagem inicial foi mantida.", ephemeral=True)
        await interaction.followup.send(f"Ticket limpo: {deleted_count} mensagem(ns) removida(s). A mensagem inicial foi mantida.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Não consegui apagar as mensagens. Confira minhas permissões neste canal.", ephemeral=True)
    except discord.HTTPException:
        log.exception("Falha ao limpar o ticket %s.", channel.id)
        await interaction.followup.send("O Discord não conseguiu limpar o ticket agora. Tente novamente.", ephemeral=True)


@bot.tree.command(name="registrar-farme", description="Registra a entrega do farm dentro do seu ticket Farme.")
@app_commands.guild_only()
async def registrar_farme(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or not is_farme_ticket(channel):
        return await interaction.response.send_message("Use `/registrar-farme` dentro de um ticket Farme criado pelo bot.", ephemeral=True)
    await interaction.response.send_modal(FarmeRecordModal())


@bot.tree.command(name="painel-meta", description="Publica ou atualiza o painel da meta de R$ 100.000 da família.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def meta(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel) or not interaction.guild:
        return await interaction.response.send_message("Use este comando em um canal de texto do servidor.", ephemeral=True)
    if META_PANEL_CHANNEL_ID.isdigit() and interaction.channel.id != int(META_PANEL_CHANNEL_ID):
        target = interaction.guild.get_channel(int(META_PANEL_CHANNEL_ID))
        where = target.mention if target else "『🕹️』ᴄᴏɴᴛᴏʟᴇ-ᴅᴇ-ꜰᴀʀᴍᴇ"
        return await interaction.response.send_message(f"`/painel-meta` só pode ser usado em {where}.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    target_channel = find_meta_panel_channel(interaction.guild) or interaction.channel
    try:
        message = await publish_meta_panel(interaction.guild, target_channel)
        await bot.ensure_weekly_meta_controls(interaction.guild)
        await interaction.followup.send(f"Painel semanal publicado ou atualizado em {message.channel.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Não tenho permissão para publicar no canal escolhido.", ephemeral=True)
    except discord.HTTPException:
        log.exception("Falha ao publicar painel da meta.")
        await interaction.followup.send("O Discord não conseguiu publicar o painel agora.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN não está configurado. Copie .env.example para .env e adicione o token localmente; nunca o envie no chat.")
    bot.run(TOKEN, log_handler=None)


