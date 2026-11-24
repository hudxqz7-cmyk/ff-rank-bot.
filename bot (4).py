import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

# =====================================================
# FF RANK SYSTEM TREM
# Um único arquivo: bot.py
# Requisitos: Python 3.10+ e discord.py 2.x
# =====================================================

TOKEN = os.getenv("DISCORD_TOKEN")

ADMIN_ROLE_ID = 1574625648868327530

GENERAL_CHANNEL_ID = 1574568353350291496
SEASON_CHANNEL_ID = 1574624786045210724
ANNOUNCEMENT_CHANNEL_ID = 1574868120164765807  # reservado para avisos

SEASON_DAYS = 15
DATABASE = os.getenv("DATABASE_PATH", "ff_rank.sqlite")
BACKUP_DIR = "backups"

MAX_PLAYERS = 20
MAX_POINTS = 10000
MAX_MENTION_TEXT = 4000

Path(BACKUP_DIR).mkdir(exist_ok=True)
Path(DATABASE).parent.mkdir(parents=True, exist_ok=True)

# =====================================================
# BANCO DE DADOS
# =====================================================

db = sqlite3.connect(DATABASE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")

db.executescript("""
CREATE TABLE IF NOT EXISTS config (
    guild INTEGER PRIMARY KEY,
    start REAL,
    season INTEGER DEFAULT 1,
    win INTEGER DEFAULT 25,
    loss INTEGER DEFAULT -15,
    draw INTEGER DEFAULT 0,
    mvp INTEGER DEFAULT 10,
    last_weekly TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild INTEGER,
    season INTEGER,
    mode TEXT DEFAULT 'equipes',
    team_a TEXT,
    team_b TEXT,
    winner TEXT,
    mvp INTEGER,
    awards TEXT,
    admin INTEGER,
    created REAL,
    status TEXT DEFAULT 'confirmed'
);

CREATE TABLE IF NOT EXISTS champions (
    guild INTEGER,
    season INTEGER,
    player INTEGER,
    points INTEGER,
    ended REAL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild INTEGER,
    admin INTEGER,
    action TEXT,
    details TEXT,
    created REAL
);

CREATE TABLE IF NOT EXISTS panels (
    guild INTEGER,
    kind TEXT,
    channel INTEGER,
    message INTEGER,
    PRIMARY KEY(guild, kind)
);

-- Ajustes feitos por administradores. São somados ao XP das partidas.
CREATE TABLE IF NOT EXISTS xp_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild INTEGER NOT NULL,
    season INTEGER NOT NULL,
    player INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    admin INTEGER NOT NULL,
    reason TEXT DEFAULT '',
    created REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_guild_status
ON matches(guild, status);

CREATE INDEX IF NOT EXISTS idx_adjustments_guild_player
ON xp_adjustments(guild, player);
""")

def migrate():
    migrations = {
        "matches": {
            "mode": "TEXT DEFAULT 'equipes'",
            "awards": "TEXT"
        },
        "config": {
            "last_weekly": "TEXT"
        }
    }
    for table, columns in migrations.items():
        existing = {
            row["name"]
            for row in db.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in columns.items():
            if name not in existing:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )
    db.commit()

migrate()

# =====================================================
# BOT
# =====================================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
drafts = {}
synced = False

# =====================================================
# UTILIDADES
# =====================================================

def now():
    return datetime.now(timezone.utc).timestamp()

def admin(member):
    return (
        isinstance(member, discord.Member)
        and (
            member.guild_permissions.administrator
            or any(role.id == ADMIN_ROLE_ID for role in member.roles)
        )
    )

def cfg(guild_id):
    row = db.execute(
        "SELECT * FROM config WHERE guild=?",
        (guild_id,)
    ).fetchone()

    if row is None:
        db.execute(
            "INSERT INTO config(guild, start) VALUES (?, ?)",
            (guild_id, now())
        )
        db.commit()
        return cfg(guild_id)

    return row

def audit(guild_id, user_id, action, details=""):
    db.execute("""
        INSERT INTO audit(guild, admin, action, details, created)
        VALUES (?, ?, ?, ?, ?)
    """, (guild_id, user_id, action, str(details), now()))
    db.commit()

def make_embed(title, description=""):
    e = discord.Embed(
        title=title,
        description=description,
        color=0x21172D,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text="FF RANK SYSTEM TREM • COMPETITIVE")
    return e

def parse_players(value):
    """Aceita menções (@membro) e IDs numéricos do Discord."""
    found = re.findall(r"<@!?(\d{15,20})>|(?<!\d)(\d{17,20})(?!\d)", value)
    ids = []
    for mention_id, raw_id in found:
        player_id = int(mention_id or raw_id)
        if player_id not in ids:
            ids.append(player_id)
    return ids

def current_season(guild_id):
    return rotate(guild_id)

def get_member_name(guild, player_id):
    member = guild.get_member(player_id)
    return member.mention if member else f"<@{player_id}>"

# =====================================================
# PONTUAÇÃO
# =====================================================

def calculate(guild_id, data):
    c = cfg(guild_id)
    awards = {}

    for team, group in (("A", data["A"]), ("B", data["B"])):
        for player in group:
            won = data["winner"] == team
            draw = data["winner"] == "DRAW"
            is_mvp = player == data["mvp"]

            points = (
                c["draw"] if draw
                else c["win"] if won
                else c["loss"]
            )
            if is_mvp:
                points += c["mvp"]

            awards[str(player)] = {
                "points": points,
                "wins": int(won),
                "losses": int(not won and not draw),
                "draws": int(draw),
                "mvps": int(is_mvp)
            }

    return awards

# =====================================================
# ESTATÍSTICAS E PERFIS
# =====================================================

def stats(guild_id, season=None):
    if season is None:
        rows = db.execute("""
            SELECT * FROM matches
            WHERE guild=? AND status='confirmed'
        """, (guild_id,)).fetchall()
        adjustments = db.execute("""
            SELECT player, SUM(amount) AS total
            FROM xp_adjustments
            WHERE guild=?
            GROUP BY player
        """, (guild_id,)).fetchall()
    else:
        rows = db.execute("""
            SELECT * FROM matches
            WHERE guild=? AND season=? AND status='confirmed'
        """, (guild_id, season)).fetchall()
        adjustments = db.execute("""
            SELECT player, SUM(amount) AS total
            FROM xp_adjustments
            WHERE guild=? AND season=?
            GROUP BY player
        """, (guild_id, season)).fetchall()

    result = {}

    for match in rows:
        team_a = json.loads(match["team_a"] or "[]")
        team_b = json.loads(match["team_b"] or "[]")
        awards = json.loads(match["awards"] or "{}")

        if not awards:
            awards = calculate(guild_id, {
                "A": team_a,
                "B": team_b,
                "winner": match["winner"],
                "mvp": match["mvp"]
            })

        for player in team_a + team_b:
            p = result.setdefault(player, {
                "points": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "mvps": 0,
                "matches": 0
            })
            award = awards.get(str(player), {})
            for key in ("points", "wins", "losses", "draws", "mvps"):
                p[key] += int(award.get(key, 0))
            p["matches"] += 1

    for row in adjustments:
        player = int(row["player"])
        p = result.setdefault(player, {
            "points": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "mvps": 0,
            "matches": 0
        })
        p["points"] += int(row["total"] or 0)

    return sorted(
        result.items(),
        key=lambda item: (
            item[1]["points"],
            item[1]["wins"],
            item[1]["mvps"]
        ),
        reverse=True
    )

def profile_embed(guild, player_id):
    all_stats = stats(guild.id)
    season_number = rotate(guild.id)
    season_stats = stats(guild.id, season_number)

    overall = dict(all_stats).get(player_id, {
        "points": 0, "wins": 0, "losses": 0,
        "draws": 0, "mvps": 0, "matches": 0
    })
    current = dict(season_stats).get(player_id, {
        "points": 0, "wins": 0, "losses": 0,
        "draws": 0, "mvps": 0, "matches": 0
    })

    position = next(
        (i for i, (pid, _) in enumerate(all_stats, 1) if pid == player_id),
        None
    )
    member = guild.get_member(player_id)
    name = member.display_name if member else f"ID {player_id}"

    e = make_embed(f"👤 PERFIL — {name}")
    e.add_field(
        name="🏆 XP total",
        value=f"**{overall['points']} pontos**",
        inline=True
    )
    e.add_field(
        name="📊 Posição geral",
        value=f"**#{position}**" if position else "Sem ranking",
        inline=True
    )
    e.add_field(
        name="⭐ MVPs",
        value=str(overall["mvps"]),
        inline=True
    )
    e.add_field(
        name="⚔️ Partidas",
        value=str(overall["matches"]),
        inline=True
    )
    e.add_field(
        name="✅ Vitórias",
        value=str(overall["wins"]),
        inline=True
    )
    e.add_field(
        name="❌ Derrotas",
        value=str(overall["losses"]),
        inline=True
    )
    e.add_field(
        name="🤝 Empates",
        value=str(overall["draws"]),
        inline=True
    )
    e.add_field(
        name=f"📅 Temporada #{season_number}",
        value=(
            f"XP: **{current['points']}**\n"
            f"Vitórias: **{current['wins']}** • "
            f"Derrotas: **{current['losses']}**\n"
            f"MVPs: **{current['mvps']}**"
        ),
        inline=False
    )
    if member and member.display_avatar:
        e.set_thumbnail(url=member.display_avatar.url)
    return e

# =====================================================
# TEMPORADAS
# =====================================================

def rotate(guild_id):
    c = cfg(guild_id)
    start = c["start"]
    number = c["season"]
    current = now()
    duration = SEASON_DAYS * 86400

    while current >= start + duration:
        old = stats(guild_id, number)
        champion = old[0] if old else None

        db.execute("""
            INSERT INTO champions(guild, season, player, points, ended)
            VALUES (?, ?, ?, ?, ?)
        """, (
            guild_id,
            number,
            champion[0] if champion else None,
            champion[1]["points"] if champion else 0,
            start + duration
        ))
        start += duration
        number += 1

    db.execute(
        "UPDATE config SET start=?, season=? WHERE guild=?",
        (start, number, guild_id)
    )
    db.commit()
    return number

# =====================================================
# RANKING
# =====================================================

def ranking_embed(guild_id, season=None):
    data = stats(guild_id, season)
    title = (
        "🏆 RANKING GERAL"
        if season is None
        else f"⚔️ TEMPORADA #{season}"
    )
    e = make_embed(title)

    if not data:
        e.description = "Nenhuma partida registrada ainda."
        return e

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (player_id, p) in enumerate(data[:20]):
        place = medals[i] if i < 3 else f"**#{i + 1}**"
        lines.append(
            f"{place} <@{player_id}> — **{p['points']} XP** "
            f"• {p['wins']}V / {p['losses']}D • ⭐ {p['mvps']} MVP"
        )
    e.description = "\n".join(lines)
    return e

# =====================================================
# VALIDAÇÃO E REGISTRO DE PARTIDA
# =====================================================

def validate_match(data):
    a = data["A"]
    b = data["B"]

    if not a or not b:
        return False, "Informe pelo menos um jogador em cada time."
    if len(a + b) > MAX_PLAYERS:
        return False, f"O limite é de {MAX_PLAYERS} jogadores por partida."
    if len(set(a + b)) != len(a) + len(b):
        return False, "Um jogador não pode estar nos dois times ou repetido."
    if data["winner"] not in ("A", "B", "DRAW"):
        return False, "Vencedor inválido."
    if data["mvp"] not in a + b:
        return False, "O MVP precisa estar em um dos dois times."
    return True, ""

def save_match(guild_id, user_id, data, season):
    awards = calculate(guild_id, data)
    cursor = db.execute("""
        INSERT INTO matches(
            guild, season, mode, team_a, team_b, winner,
            mvp, awards, admin, created, status
        )
        VALUES (?, ?, 'equipes', ?, ?, ?, ?, ?, ?, ?, 'confirmed')
    """, (
        guild_id,
        season,
        json.dumps(data["A"]),
        json.dumps(data["B"]),
        data["winner"],
        data["mvp"],
        json.dumps(awards),
        user_id,
        now()
    ))
    db.commit()
    return cursor.lastrowid

# =====================================================
# PAINÉIS AUTOMÁTICOS
# =====================================================

async def refresh(guild):
    season = rotate(guild.id)
    panels = [
        ("general", GENERAL_CHANNEL_ID, ranking_embed(guild.id)),
        ("season", SEASON_CHANNEL_ID, ranking_embed(guild.id, season))
    ]

    for kind, channel_id, message_embed in panels:
        try:
            channel = guild.get_channel(channel_id)
            if channel is None:
                channel = await bot.fetch_channel(channel_id)

            saved = db.execute("""
                SELECT * FROM panels WHERE guild=? AND kind=?
            """, (guild.id, kind)).fetchone()

            message = None
            if saved:
                try:
                    message = await channel.fetch_message(saved["message"])
                except (discord.NotFound, discord.Forbidden):
                    message = None

            if message:
                await message.edit(embed=message_embed)
            else:
                message = await channel.send(embed=message_embed)
                db.execute("""
                    INSERT OR REPLACE INTO panels(guild, kind, channel, message)
                    VALUES (?, ?, ?, ?)
                """, (guild.id, kind, channel.id, message.id))
                db.commit()

        except Exception as error:
            print(f"Erro ao atualizar painel {kind} no servidor {guild.id}: {error}")

# =====================================================
# BACKUP
# =====================================================

def backup():
    filename = datetime.now().strftime(
        f"{BACKUP_DIR}/ff-rank-%Y%m%d-%H%M%S.sqlite"
    )
    target = sqlite3.connect(filename)
    try:
        db.backup(target)
    finally:
        target.close()

    files = sorted(
        Path(BACKUP_DIR).glob("*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True
    )
    for old in files[30:]:
        old.unlink()
    return filename

# =====================================================
# MODAL DE REGISTRO
# =====================================================

class MatchModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Registrar partida")
        self.vencedores = discord.ui.TextInput(
            label="Time vencedor — marque os @",
            placeholder="@jogador1 @jogador2",
            style=discord.TextStyle.paragraph,
            max_length=MAX_MENTION_TEXT
        )
        self.perdedores = discord.ui.TextInput(
            label="Time perdedor — marque os @",
            placeholder="@jogador3 @jogador4",
            style=discord.TextStyle.paragraph,
            max_length=MAX_MENTION_TEXT
        )
        self.mvp = discord.ui.TextInput(
            label="MVP da partida — marque o @",
            placeholder="@jogador",
            max_length=100
        )
        self.add_item(self.vencedores)
        self.add_item(self.perdedores)
        self.add_item(self.mvp)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not admin(interaction.user):
            return await interaction.response.send_message(
                "Você não tem permissão para registrar partidas.",
                ephemeral=True
            )

        winners = parse_players(self.vencedores.value)
        losers = parse_players(self.perdedores.value)
        mvp_ids = parse_players(self.mvp.value)

        if len(mvp_ids) != 1:
            return await interaction.response.send_message(
                "Marque exatamente um jogador como MVP.",
                ephemeral=True
            )

        all_ids = winners + losers + mvp_ids
        for player_id in all_ids:
            member = interaction.guild.get_member(player_id)
            if member is None:
                try:
                    await interaction.guild.fetch_member(player_id)
                except discord.NotFound:
                    return await interaction.response.send_message(
                        f"O membro <@{player_id}> não foi encontrado neste servidor.",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    return await interaction.response.send_message(
                        "Não consegui verificar um dos membros. Tente novamente.",
                        ephemeral=True
                    )

        data = {
            "mode": "equipes",
            "A": winners,
            "B": losers,
            "winner": "A",
            "mvp": mvp_ids[0]
        }
        valid, error_message = validate_match(data)
        if not valid:
            return await interaction.response.send_message(
                f"❌ {error_message}",
                ephemeral=True
            )

        key = interaction.id
        drafts[interaction.user.id] = {"key": key, "data": data}
        awards = calculate(interaction.guild.id, data)
        lines = []
        for player_id, award in awards.items():
            label = "⭐ MVP • " if award["mvps"] else ""
            lines.append(f"<@{player_id}> — {label}**{award['points']:+} XP**")

        preview = make_embed(
            "📋 CONFIRMAR PARTIDA",
            "**Vencedores:** " + ", ".join(f"<@{p}>" for p in winners) +
            "\n**Perdedores:** " + ", ".join(f"<@{p}>" for p in losers) +
            f"\n**MVP:** <@{mvp_ids[0]}>\n\n" + "\n".join(lines)
        )
        await interaction.response.send_message(
            embed=preview,
            view=ConfirmView(key),
            ephemeral=True
        )

# =====================================================
# CONFIRMAÇÃO DA PARTIDA
# =====================================================

class ConfirmView(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=180)
        self.key = key

    @discord.ui.button(
        label="Confirmar partida",
        emoji="🏆",
        style=discord.ButtonStyle.success
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        draft = drafts.get(interaction.user.id)
        if not draft or draft["key"] != self.key:
            return await interaction.response.send_message(
                "Prévia expirada. Registre novamente.",
                ephemeral=True
            )
        if not interaction.guild or not admin(interaction.user):
            return await interaction.response.send_message(
                "Sem permissão.",
                ephemeral=True
            )

        drafts.pop(interaction.user.id, None)
        await interaction.response.defer(ephemeral=True)

        try:
            season = rotate(interaction.guild.id)
            match_id = save_match(
                interaction.guild.id,
                interaction.user.id,
                draft["data"],
                season
            )
            audit(
                interaction.guild.id,
                interaction.user.id,
                "partida_confirmada",
                f"Partida #{match_id}"
            )
            await refresh(interaction.guild)
            await interaction.followup.send(
                f"✅ Partida **#{match_id}** registrada! "
                "XP, perfis e rankings atualizados.",
                ephemeral=True
            )
        except Exception as error:
            print(f"Erro ao salvar partida: {error}")
            await interaction.followup.send(
                "Não foi possível salvar a partida. Verifique os logs.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Cancelar",
        style=discord.ButtonStyle.danger
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        draft = drafts.get(interaction.user.id)
        if draft and draft["key"] == self.key:
            drafts.pop(interaction.user.id, None)
        await interaction.response.edit_message(
            content="Registro cancelado.",
            embed=None,
            view=None
        )

# =====================================================
# MODAL DE CONFIGURAÇÃO DE PONTOS
# =====================================================

class PointsModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Configurar pontuação")
        self.win = discord.ui.TextInput(label="XP por vitória", default="25")
        self.loss = discord.ui.TextInput(label="XP por derrota", default="-15")
        self.draw = discord.ui.TextInput(label="XP por empate", default="0")
        self.mvp = discord.ui.TextInput(label="Bônus de MVP", default="10")
        self.add_item(self.win)
        self.add_item(self.loss)
        self.add_item(self.draw)
        self.add_item(self.mvp)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not admin(interaction.user):
            return await interaction.response.send_message("Sem permissão.", ephemeral=True)

        try:
            values = [int(field.value) for field in (self.win, self.loss, self.draw, self.mvp)]
            if any(abs(value) > MAX_POINTS for value in values):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                f"Use números inteiros entre -{MAX_POINTS} e {MAX_POINTS}.",
                ephemeral=True
            )

        cfg(interaction.guild.id)
        db.execute("""
            UPDATE config SET win=?, loss=?, draw=?, mvp=?
            WHERE guild=?
        """, (*values, interaction.guild.id))
        db.commit()
        audit(interaction.guild.id, interaction.user.id, "pontuacao_alterada", values)

        await interaction.response.send_message(
            "✅ Pontuação atualizada. Partidas já registradas mantêm o XP original.",
            ephemeral=True
        )

# =====================================================
# PAINEL ADMINISTRATIVO
# =====================================================

class AdminPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction):
        if not admin(interaction.user):
            await interaction.response.send_message(
                "🔒 Apenas administradores ou o cargo configurado podem usar este painel.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Registrar partida",
        emoji="⚔️",
        style=discord.ButtonStyle.success,
        custom_id="ffrank:register"
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MatchModal())

    @discord.ui.button(
        label="Atualizar rankings",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        custom_id="ffrank:update"
    )
    async def update(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await refresh(interaction.guild)
        await interaction.followup.send("✅ Rankings atualizados!", ephemeral=True)

    @discord.ui.button(
        label="Backup",
        emoji="💾",
        style=discord.ButtonStyle.secondary,
        custom_id="ffrank:backup"
    )
    async def backup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            filename = await asyncio.to_thread(backup)
            await interaction.followup.send(
                f"✅ Backup realizado: `{filename}`",
                ephemeral=True
            )
        except Exception as error:
            await interaction.followup.send(f"Erro no backup: {error}", ephemeral=True)

    @discord.ui.button(
        label="Pontuação",
        emoji="⚙️",
        style=discord.ButtonStyle.secondary,
        custom_id="ffrank:points"
    )
    async def points(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PointsModal())

    @discord.ui.button(
        label="Ver ranking",
        emoji="🏆",
        style=discord.ButtonStyle.secondary,
        custom_id="ffrank:ranking"
    )
    async def ranking_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=ranking_embed(interaction.guild.id),
            ephemeral=True
        )

# =====================================================
# COMANDOS SLASH
# =====================================================

@bot.tree.command(name="painel", description="Abrir painel administrativo FF Rank (uso privado)")
async def painel(interaction: discord.Interaction):
    if not interaction.guild or not admin(interaction.user):
        return await interaction.response.send_message("Sem permissão.", ephemeral=True)
    await interaction.response.send_message(
        embed=make_embed(
            "🏆 FF RANK SYSTEM TREM",
            "Painel administrativo para partidas, pontuação e rankings."
        ),
        view=AdminPanel(),
        ephemeral=True
    )

@bot.tree.command(
    name="painel_fixo",
    description="Publicar ou atualizar o painel fixo de administração em um canal"
)
@app_commands.describe(canal="Canal onde o painel administrativo ficará fixado")
async def painel_fixo(
    interaction: discord.Interaction,
    canal: discord.TextChannel
):
    if not interaction.guild or not admin(interaction.user):
        return await interaction.response.send_message(
            "Somente administradores podem configurar o painel.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    panel_embed = make_embed(
        "🏆 FF RANK SYSTEM TREM — PAINEL ADMINISTRATIVO",
        "Use os botões abaixo para administrar o ranking.\\n\\n"
        "⚔️ **Registrar partida:** informe os @ do time vencedor, "
        "do time perdedor e do MVP.\\n"
        "🔄 **Atualizar rankings:** atualiza os painéis de ranking.\\n"
        "💾 **Backup:** cria uma cópia do banco de dados.\\n"
        "⚙️ **Pontuação:** configura XP de vitória, derrota, empate e MVP.\\n"
        "🏆 **Ver ranking:** consulta o ranking atual.\\n\\n"
        "Este painel continua funcionando após reinicializações do bot."
    )

    try:
        saved = db.execute(
            "SELECT * FROM panels WHERE guild=? AND kind='admin'",
            (interaction.guild.id,)
        ).fetchone()

        message = None
        if saved:
            try:
                old_channel = interaction.guild.get_channel(saved["channel"])
                if old_channel is None:
                    old_channel = await bot.fetch_channel(saved["channel"])
                message = await old_channel.fetch_message(saved["message"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        # Se já existe um painel, atualiza-o no canal escolhido.
        if message and message.channel.id == canal.id:
            await message.edit(embed=panel_embed, view=AdminPanel())
            action = "Painel fixo atualizado"
        else:
            message = await canal.send(embed=panel_embed, view=AdminPanel())
            action = "Painel fixo publicado"

        db.execute("""
            INSERT OR REPLACE INTO panels(guild, kind, channel, message)
            VALUES (?, 'admin', ?, ?)
        """, (interaction.guild.id, canal.id, message.id))
        db.commit()

        audit(
            interaction.guild.id,
            interaction.user.id,
            "painel_fixo_configurado",
            f"Canal {canal.id}; mensagem {message.id}"
        )

        await interaction.followup.send(
            f"✅ {action} em {canal.mention}.\\n"
            "Os botões de administração continuarão ativos após reiniciar o bot.",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Não tenho permissão para enviar mensagens e incorporar links nesse canal.",
            ephemeral=True
        )
    except Exception as error:
        print(f"Erro ao configurar painel fixo: {error}")
        await interaction.followup.send(
            "Não foi possível publicar o painel. Confira as permissões do bot e os logs.",
            ephemeral=True
        )


@bot.tree.command(name="perfil", description="Ver seu perfil ou o perfil de outro jogador")
@app_commands.describe(membro="Membro cujo perfil deseja consultar (opcional)")
async def perfil(interaction: discord.Interaction, membro: discord.Member = None):
    if not interaction.guild:
        return await interaction.response.send_message("Use este comando dentro de um servidor.", ephemeral=True)
    target = membro or interaction.user
    await interaction.response.send_message(
        embed=profile_embed(interaction.guild, target.id)
    )

@bot.tree.command(name="ranking", description="Consultar ranking geral")
async def ranking_command(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
    await interaction.response.send_message(embed=ranking_embed(interaction.guild.id))

@bot.tree.command(name="temporada", description="Consultar ranking da temporada atual")
async def temporada_command(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
    season = rotate(interaction.guild.id)
    await interaction.response.send_message(
        embed=ranking_embed(interaction.guild.id, season)
    )

@bot.tree.command(name="partida", description="Consultar uma partida registrada")
@app_commands.describe(partida_id="Número da partida")
async def partida_command(interaction: discord.Interaction, partida_id: int):
    if not interaction.guild:
        return await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)

    match = db.execute("""
        SELECT * FROM matches
        WHERE id=? AND guild=? AND status='confirmed'
    """, (partida_id, interaction.guild.id)).fetchone()

    if not match:
        return await interaction.response.send_message("Partida não encontrada.", ephemeral=True)

    team_a = json.loads(match["team_a"] or "[]")
    team_b = json.loads(match["team_b"] or "[]")
    e = make_embed(
        f"⚔️ PARTIDA #{match['id']}",
        f"**Time vencedor:** {', '.join(f'<@{p}>' for p in team_a)}\n"
        f"**Time perdedor:** {', '.join(f'<@{p}>' for p in team_b)}\n"
        f"**MVP:** <@{match['mvp']}>\n"
        f"**Temporada:** #{match['season']}"
    )
    await interaction.response.send_message(embed=e, ephemeral=True)

# =====================================================
# COMANDOS DE XP — SOMENTE ADMIN
# =====================================================

async def adjust_xp(interaction, membro, amount, action, reason="Ajuste administrativo"):
    if not interaction.guild or not admin(interaction.user):
        return await interaction.response.send_message("Somente administradores podem ajustar XP.", ephemeral=True)
    if membro.bot:
        return await interaction.response.send_message("Não é possível ajustar XP de bots.", ephemeral=True)
    if amount == 0 or abs(amount) > MAX_POINTS:
        return await interaction.response.send_message(
            f"O valor deve ser diferente de zero e não passar de {MAX_POINTS}.",
            ephemeral=True
        )

    season = rotate(interaction.guild.id)
    db.execute("""
        INSERT INTO xp_adjustments(guild, season, player, amount, admin, reason, created)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        interaction.guild.id,
        season,
        membro.id,
        amount,
        interaction.user.id,
        reason[:300],
        now()
    ))
    db.commit()
    audit(
        interaction.guild.id,
        interaction.user.id,
        action,
        f"{membro.id}: {amount} XP; motivo: {reason}"
    )
    await refresh(interaction.guild)

    total = dict(stats(interaction.guild.id)).get(membro.id, {}).get("points", 0)
    await interaction.response.send_message(
        f"✅ Ajuste aplicado em {membro.mention}: **{amount:+} XP**.\n"
        f"XP total atual: **{total}**.",
        ephemeral=True
    )

@bot.tree.command(name="addxp", description="Adicionar XP a um membro (admin)")
@app_commands.describe(membro="Membro que receberá XP", quantidade="Quantidade de XP a adicionar", motivo="Motivo do ajuste")
async def addxp(interaction: discord.Interaction, membro: discord.Member, quantidade: app_commands.Range[int, 1, MAX_POINTS], motivo: str = "Adição administrativa"):
    await adjust_xp(interaction, membro, quantidade, "add_xp", motivo)

@bot.tree.command(name="removexp", description="Remover XP de um membro (admin)")
@app_commands.describe(membro="Membro que perderá XP", quantidade="Quantidade de XP a remover", motivo="Motivo do ajuste")
async def removexp(interaction: discord.Interaction, membro: discord.Member, quantidade: app_commands.Range[int, 1, MAX_POINTS], motivo: str = "Remoção administrativa"):
    await adjust_xp(interaction, membro, -quantidade, "remove_xp", motivo)

@bot.tree.command(name="setxp", description="Definir o XP total de um membro (admin)")
@app_commands.describe(membro="Membro cujo XP será definido", quantidade="Novo XP total", motivo="Motivo do ajuste")
async def setxp(interaction: discord.Interaction, membro: discord.Member, quantidade: app_commands.Range[int, 0, MAX_POINTS], motivo: str = "Definição administrativa"):
    if not interaction.guild or not admin(interaction.user):
        return await interaction.response.send_message("Somente administradores podem ajustar XP.", ephemeral=True)
    if membro.bot:
        return await interaction.response.send_message("Não é possível ajustar XP de bots.", ephemeral=True)

    current_total = dict(stats(interaction.guild.id)).get(membro.id, {}).get("points", 0)
    difference = quantidade - current_total
    season = rotate(interaction.guild.id)

    if difference != 0:
        db.execute("""
            INSERT INTO xp_adjustments(guild, season, player, amount, admin, reason, created)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            interaction.guild.id, season, membro.id, difference,
            interaction.user.id, motivo[:300], now()
        ))
        db.commit()

    audit(
        interaction.guild.id, interaction.user.id, "set_xp",
        f"{membro.id}: {current_total} -> {quantidade}; {motivo}"
    )
    await refresh(interaction.guild)
    await interaction.response.send_message(
        f"✅ XP de {membro.mention} definido para **{quantidade}**.",
        ephemeral=True
    )

# =====================================================
# TAREFAS AUTOMÁTICAS
# =====================================================

@tasks.loop(hours=24)
async def daily_backup():
    await asyncio.to_thread(backup)

@daily_backup.before_loop
async def before_backup():
    await bot.wait_until_ready()

@tasks.loop(hours=1)
async def update_seasons():
    for guild in bot.guilds:
        try:
            old_season = cfg(guild.id)["season"]
            new_season = rotate(guild.id)
            if new_season != old_season:
                await refresh(guild)
        except Exception as error:
            print(f"Erro na temporada de {guild.id}: {error}")

@update_seasons.before_loop
async def before_seasons():
    await bot.wait_until_ready()

# =====================================================
# INICIALIZAÇÃO
# =====================================================

@bot.event
async def on_ready():
    global synced
    print("=" * 45)
    print("FF RANK SYSTEM TREM")
    print(f"Bot conectado: {bot.user}")
    print(f"Servidores: {len(bot.guilds)}")
    print("=" * 45)

    if not synced:
        bot.add_view(AdminPanel())
        try:
            await bot.tree.sync()
            synced = True
            print("Comandos slash sincronizados.")
        except Exception as error:
            print(f"Erro ao sincronizar comandos: {error}")

    for guild in bot.guilds:
        cfg(guild.id)
        rotate(guild.id)

    if not daily_backup.is_running():
        daily_backup.start()
    if not update_seasons.is_running():
        update_seasons.start()

# =====================================================
# TRATAMENTO DE ERROS
# =====================================================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Erro de comando: {repr(error)}")
    message = "Ocorreu um erro ao executar o comando."
    if isinstance(error, app_commands.MissingPermissions):
        message = "Você não tem permissão para isso."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass

# =====================================================
# EXECUÇÃO
# =====================================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Configure a variável DISCORD_TOKEN no Railway.")
    bot.run(TOKEN)
