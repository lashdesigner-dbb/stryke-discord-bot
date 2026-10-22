Warning: truncated output (original token count: 23325)
Total output lines: 1808

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
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(CloseTicketView())
        self.add_view(MetaTrackerView())
        self.add_view(IdentificationPanelView())
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
            await self.ensure_weekly_meta_controls(guild)
            repaired = await reconcile_farme_ticket_permissions(guild)
            if repaired:
                log.info("Permissões de acesso e histórico atualizadas em %d ticket(s) Farme.", repaired)
            self.restore_absence_cleanup(guild)
            await reconcile_meta_state(guild)
        if not refresh_site_meta_cache.is_running():
            refresh_site_meta_cache.start()

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

    async def archive_public_identifications(
        self,
        public_channel: discord.TextChannel,
        storage_channel: discord.TextChannel,
    ) -> bool:
        record_pattern = re.compile(r"^🪪 Cadastro de identificação #(\d+)$")
        archived_numbers: set[str] = set()
        try:
            async for saved_message in storage_channel.history(limit=None):
                for embed in saved_message.embeds:
                    match = record_pattern.match(embed.title or "")
                    if match:
                        archived_numbers.add(match.group(1))

            async for message in public_channel.history(limit=None, oldest_first=True):
                if message.author != self.user:
                    continue
                for embed in message.embeds:
                    match = record_pattern.match(embed.title or "")
                    if not match or match.group(1) in archived_numbers:
                        continue
                    # Copy each existing registration before the public channel is purged.
                    await storage_channel.send(
                        embed=embed.copy(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    archived_numbers.add(match.group(1))
            return True
        except discord.Forbidden:
            log.exception("Não foi possível ler ou arquivar os cadastros antes da limpeza do canal público.")
            return False
        except discord.HTTPException:
            log.exception("Falha ao arquivar os cadastros; as mensagens públicas serão preservadas.")
            return False

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
 …11325 tokens truncated…confira minha permissão e a hierarquia dos cargos")
                    log.exception("Sem permissão para atribuir o cargo Membro após identificação #%04d.", number)
                except discord.HTTPException:
                    update_errors.append("o Discord não conseguiu atribuir o cargo `Membro`")
                    log.exception("Falha ao atribuir o cargo Membro após identificação #%04d.", number)
                else:
                    role_updated = True
            else:
                role_updated = True

        nickname = values["Nome"][:32]
        try:
            await member.edit(nick=nickname, reason=f"Identificação STRYKE #{number:04d} concluída")
        except discord.Forbidden:
            update_errors.append("não consegui atualizar seu apelido; confira minha permissão de gerenciar apelidos")
            log.exception("Sem permissão para atualizar o apelido após identificação #%04d.", number)
        except discord.HTTPException:
            update_errors.append("o Discord não conseguiu atualizar seu apelido")
            log.exception("Falha ao atualizar o apelido após identificação #%04d.", number)
        else:
            nickname_updated = True

        if role_updated and nickname_updated:
            message = f"Cadastro #{number:04d} enviado para a equipe. Cargo `Membro` atribuído e apelido atualizado para **{nickname}**."
        else:
            message = f"Cadastro #{number:04d} enviado para a equipe, mas " + "; ".join(update_errors) + "."
        await interaction.followup.send(message, ephemeral=True)


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

