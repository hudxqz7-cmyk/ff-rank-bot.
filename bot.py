from discord import app_commands
import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks

# =====================================================
# FF RANK SYSTEM TREM
# =====================================================

TOKEN = os.getenv("DISCORD_TOKEN")

ADMIN_ROLE_ID = 1574625648868327530

GENERAL_CHANNEL_ID = 1574568353350291496
SEASON_CHANNEL_ID = 1574624786045210724
ANNOUNCEMENT_CHANNEL_ID = 1574868120164765807

SEASON_DAYS = 15
DATABASE = "ff_rank.sqlite"
BACKUP_DIR = "backups"

MAX_PLAYERS = 20
MAX_POINTS = 10000

Path(BACKUP_DIR).mkdir(exist_ok=True)

# =====================================================
# BANCO DE DADOS
# =====================================================

db = sqlite3.connect(DATABASE)
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
            for row in db.execute(
                f"PRAGMA table_info({table})"
            )
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

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

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
        and any(
            role.id == ADMIN_ROLE_ID
            for role in member.roles
        )
    )

def cfg(guild):
    row = db.execute(
        "SELECT * FROM config WHERE guild=?",
        (guild,)
    ).fetchone()

    if row is None:
        db.execute("""
            INSERT INTO config(guild, start)
            VALUES (?, ?)
        """, (guild, now()))

        db.commit()

        return cfg(guild)

    return row

def audit(guild, user, action, details=""):
    db.execute("""
        INSERT INTO audit(
            guild, admin, action, details, created
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        guild, user, action,
        str(details), now()
    ))

    db.commit()

def embed(title, description=""):
    e = discord.Embed(
        title=title,
        description=description,
        color=0x21172D,
        timestamp=datetime.now(timezone.utc)
    )

    e.set_footer(
        text="FF RANK SYSTEM TREM • COMPETITIVE"
    )

    return e

def players(value):
    return list(dict.fromkeys(
        int(x)
        for x in re.findall(r"\d{17,20}", value)
    ))

# =====================================================
# PONTUAÇÃO
# =====================================================

def calculate(guild, data):
    c = cfg(guild)
    awards = {}

    for team, group in (
        ("A", data["A"]),
        ("B", data["B"])
    ):
        for player in group:
            win = data["winner"] == team
            draw = data["winner"] == "DRAW"
            is_mvp = player == data["mvp"]

            points = (
                c["draw"] if draw
                else c["win"] if win
                else c["loss"]
            )

            if is_mvp:
                points += c["mvp"]

            awards[str(player)] = {
                "points": points,
                "wins": int(win),
                "losses": int(not win and not draw),
                "draws": int(draw),
                "mvps": int(is_mvp)
            }

    return awards

# =====================================================
# ESTATÍSTICAS
# =====================================================

def stats(guild, season=None):
    if season is None:
        rows = db.execute("""
            SELECT * FROM matches
            WHERE guild=? AND status='confirmed'
        """, (guild,)).fetchall()
    else:
        rows = db.execute("""
            SELECT * FROM matches
            WHERE guild=?
            AND season=?
            AND status='confirmed'
        """, (guild, season)).fetchall()

    result = {}

    for match in rows:
        a = json.loads(match["team_a"])
        b = json.loads(match["team_b"])

        awards = json.loads(
            match["awards"] or "{}"
        )

        if not awards:
            awards = calculate(guild, {
                "A": a,
                "B": b,
                "winner": match["winner"],
                "mvp": match["mvp"]
            })

        for player in a + b:
            p = result.setdefault(player, {
                "points": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "mvps": 0,
                "matches": 0
            })

            award = awards.get(str(player), {})

            for key in (
                "points", "wins", "losses",
                "draws", "mvps"
            ):
                p[key] += award.get(key, 0)

            p["matches"] += 1

    return sorted(
        result.items(),
        key=lambda x: (
            x[1]["points"],
            x[1]["wins"],
            x[1]["mvps"]
        ),
        reverse=True
    )

# =====================================================
# TEMPORADAS
# =====================================================

def rotate(guild):
    c = cfg(guild)

    start = c["start"]
    number = c["season"]

    current = now()
    duration = SEASON_DAYS * 86400

    while current >= start + duration:
        old = stats(guild, number)
        champion = old[0] if old else None

        db.execute("""
            INSERT INTO champions(
                guild, season, player, points, ended
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            guild,
            number,
            champion[0] if champion else None,
            champion[1]["points"] if champion else 0,
            start + duration
        ))

        start += duration
        number += 1

    db.execute("""
        UPDATE config
        SET start=?, season=?
        WHERE guild=?
    """, (start, number, guild))

    db.commit()

    return number

# =====================================================
# RANKING
# =====================================================

def ranking(guild, season=None):
    data = stats(guild, season)

    title = (
        "🏆 RANKING GERAL"
        if season is None
        else f"⚔️ TEMPORADA #{season}"
    )

    e = embed(title)

    if not data:
        e.description = "Nenhuma partida registrada."
        return e

    medals = ["🥇", "🥈", "🥉"]

    lines = []

    for i, (player, p) in enumerate(data[:20]):
        position = (
            medals[i]
            if i < 3
            else f"#{i+1}"
        )

        lines.append(
            f"{position} <@{player}> — "
            f"**{p['points']} pontos**"
        )

    e.description = "\n".join(lines)

    return e

# =====================================================
# SALVAR PARTIDA
# =====================================================

def validate(data):
    a = data["A"]
    b = data["B"]

    if not a or not b:
        return False

    if len(a + b) > MAX_PLAYERS:
        return False

    if len(set(a + b)) != len(a + b):
        return False

    if data["mode"] == "1v1":
        if len(a) != 1 or len(b) != 1:
            return False
    elif data["mode"] != "equipes":
        return False

    if data["winner"] not in (
        "A", "B", "DRAW"
    ):
        return False

    return data["mvp"] in a + b

def save_match(
    guild,
    user,
    data,
    season,
    match_id=None
):
    awards = calculate(guild, data)

    if match_id:
        cursor = db.execute("""
            UPDATE matches SET
                mode=?,
                team_a=?,
                team_b=?,
                winner=?,
                mvp=?,
                awards=?,
                admin=?
            WHERE id=?
            AND guild=?
            AND status='confirmed'
        """, (
            data["mode"],
            json.dumps(data["A"]),
            json.dumps(data["B"]),
            data["winner"],
            data["mvp"],
            json.dumps(awards),
            user,
            match_id,
            guild
        ))

        if cursor.rowcount == 0:
            raise ValueError(
                "Partida não encontrada."
            )

    else:
        cursor = db.execute("""
            INSERT INTO matches(
                guild, season, mode,
                team_a, team_b, winner,
                mvp, awards, admin,
                created, status
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, 'confirmed'
            )
        """, (
            guild,
            season,
            data["mode"],
            json.dumps(data["A"]),
            json.dumps(data["B"]),
            data["winner"],
            data["mvp"],
            json.dumps(awards),
            user,
            now()
        ))

        match_id = cursor.lastrowid

    db.commit()

    return match_id

# =====================================================
# PAINÉIS AUTOMÁTICOS
# =====================================================

async def refresh(guild):
    season = rotate(guild.id)

    panels = [
        ("general", GENERAL_CHANNEL_ID,
         ranking(guild.id)),

        ("season", SEASON_CHANNEL_ID,
         ranking(guild.id, season))
    ]

    for kind, channel_id, message_embed in panels:
        try:
            channel = (
                guild.get_channel(channel_id)
                or await guild.fetch_channel(channel_id)
            )

            saved = db.execute("""
                SELECT * FROM panels
                WHERE guild=? AND kind=?
            """, (guild.id, kind)).fetchone()

            message = None

            if saved:
                try:
                    message = await channel.fetch_message(
                        saved["message"]
                    )
                except discord.NotFound:
                    message = None

            if message:
                await message.edit(
                    embed=message_embed
                )
            else:
                message = await channel.send(
                    embed=message_embed
                )

                db.execute("""
                    INSERT OR REPLACE INTO panels
                    VALUES (?, ?, ?, ?)
                """, (
                    guild.id,
                    kind,
                    channel.id,
                    message.id
                ))

                db.commit()

        except discord.HTTPException as error:
            print(
                f"Erro ao atualizar painel {kind}: {error}"
            )

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
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )

    for old in files[30:]:
        old.unlink()

    return filename

# =====================================================
# CONFIRMAÇÃO
# =====================================================

class Confirm(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=180)
        self.key = key

    @discord.ui.button(
        label="Confirmar partida",
        emoji="🏆",
        style=discord.ButtonStyle.success
    )
    async def yes(self, interaction, button):
        d = drafts.get(interaction.user.id)

        if not d or d["key"] != self.key:
            return await interaction.response.send_message(
                "Prévia expirada.",
                ephemeral=True
            )

        if not admin(interaction.user):
            return await interaction.response.send_message(
                "Sem permissão.",
                ephemeral=True
            )

        drafts.pop(interaction.user.id, None)

        await interaction.response.defer(
            ephemeral=True
        )

        try:
            season = rotate(interaction.guild.id)

            match_id = save_match(
                interaction.guild.id,
                interaction.user.id,
                d["data"],
                season,
                d["match_id"]
            )

            audit(
                interaction.guild.id,
                interaction.user.id,
                "partida_confirmada",
                match_id
            )

            await refresh(interaction.guild)

            await interaction.followup.send(
                f"✅ Partida #{match_id} salva!",
                ephemeral=True
            )

        except Exception as error:
            await interaction.followup.send(
                f"Erro ao salvar: {error}",
                ephemeral=True
            )

    @discord.ui.button(
        label="Cancelar",
        style=discord.ButtonStyle.danger
    )
    async def no(self, interaction, button):
        d = drafts.get(interaction.user.id)

        if d and d["key"] == self.key:
            drafts.pop(interaction.user.id, None)

        await interaction.response.edit_message(
            content="Registro cancelado.",
            embed=None,
            view=None
        )

# =====================================================
# MODAL DE PARTIDA
# =====================================================

class MatchModal(discord.ui.Modal):
    def __init__(self, match_id=None):
        super().__init__(
            title="Registro de partida"
        )

        self.match_id = match_id

        self.mode = discord.ui.TextInput(
            label="Formato: 1v1 ou equipes",
            default="equipes"
        )

        self.a = discord.ui.TextInput(
            label="IDs equipe A"
        )

        self.b = discord.ui.TextInput(
            label="IDs equipe B"
        )

        self.winner = discord.ui.TextInput(
            label="Vencedor A, B ou DRAW",
            default="A"
        )

        self.mvp = discord.ui.TextInput(
            label="ID do MVP"
        )

        for item in (
            self.mode,
            self.a,
            self.b,
            self.winner,
            self.mvp
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        if not admin(interaction.user):
            return await interaction.response.send_message(
                "Sem permissão.",
                ephemeral=True
            )

        mvp = players(self.mvp.value)

        if len(mvp) != 1:
            return await interaction.response.send_message(
                "ID MVP inválido.",
                ephemeral=True
            )

        data = {
            "mode": self.mode.value.lower().strip(),
            "A": players(self.a.value),
            "B": players(self.b.value),
            "winner": self.winner.value.upper().strip(),
            "mvp": mvp[0]
        }

        if not validate(data):
            return await interaction.response.send_message(
                "Dados inválidos. Confira jogadores, "
                "formato, vencedor e MVP.",
                ephemeral=True
            )

        key = interaction.id

        drafts[interaction.user.id] = {
            "key": key,
            "data": data,
            "match_id": self.match_id
        }

        awards = calculate(
            interaction.guild.id,
            data
        )

        lines = [
            f"<@{int(player)}> — "
            f"**{award['points']:+} pontos**"
            + (
                " ⭐ MVP"
                if award["mvps"]
                else ""
            )
            for player, award in awards.items()
        ]

        await interaction.response.send_message(
            embed=embed(
                "📋 PRÉVIA DA PARTIDA",
                "\n".join(lines)
            ),
            view=Confirm(key),
            ephemeral=True
        )

# =====================================================
# MODAL DE PONTUAÇÃO
# =====================================================

class PointsModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(
            title="Configurar pontuação"
        )

        self.win = discord.ui.TextInput(
            label="Vitória",
            default="25"
        )

        self.loss = discord.ui.TextInput(
            label="Derrota",
            default="-15"
        )

        self.draw = discord.ui.TextInput(
            label="Empate",
            default="0"
        )

        self.mvp = discord.ui.TextInput(
            label="Bônus MVP",
            default="10"
        )

        for item in (
            self.win,
            self.loss,
            self.draw,
            self.mvp
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        if not admin(interaction.user):
            return await interaction.response.send_message(
                "Sem permissão.",
                ephemeral=True
            )

        try:
            values = [
                int(item.value)
                for item in (
                    self.win,
                    self.loss,
                    self.draw,
                    self.mvp
                )
            ]

            if any(
                abs(value) > MAX_POINTS
                for value in values
            ):
                raise ValueError

        except ValueError:
            return await interaction.response.send_message(
                "Valores inválidos.",
                ephemeral=True
            )

        cfg(interaction.guild.id)

        db.execute("""
            UPDATE config
            SET win=?, loss=?, draw=?, mvp=?
            WHERE guild=?
        """, (
            *values,
            interaction.guild.id
        ))

        db.commit()

        audit(
            interaction.guild.id,
            interaction.user.id,
            "pontuacao_alterada",
            values
        )

        await interaction.response.send_message(
            "✅ Pontuação atualizada. "
            "Partidas antigas preservam seus pontos.",
            ephemeral=True
        )

# =====================================================
# PAINEL ADMINISTRATIVO
# =====================================================

class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction):
        if not admin(interaction.user):
            await interaction.response.send_message(
                "🔒 Apenas Admin Ranking pode usar "
                "este painel.",
                ephemeral=True
            )
            return False

        return True

    @discord.ui.button(
        label="Registrar partida",
        emoji="⚔️",
        style=discord.ButtonStyle.success,
        custom_id="ff:register"
    )
    async def register(self, interaction, button):
        await interaction.response.send_modal(
            MatchModal()
        )

    @discord.ui.button(
        label="Atualizar rankings",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        custom_id="ff:update"
    )
    async def update(self, interaction, button):
        await interaction.response.defer(
            ephemeral=True
        )

        await refresh(interaction.guild)

        await interaction.followup.send(
            "✅ Rankings atualizados!",
            ephemeral=True
        )

    @discord.ui.button(
        label="Backup",
        emoji="💾",
        style=discord.ButtonStyle.secondary,
        custom_id="ff:backup"
    )
    async def backup_button(self, interaction, button):
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            filename = await asyncio.to_thread(backup)

            await interaction.followup.send(
                f"✅ Backup realizado: `{filename}`",
                ephemeral=True
            )
        except Exception as error:
            await interaction.followup.send(
                f"Erro no backup: {error}",
                ephemeral=True
            )

    @discord.ui.button(
        label="Pontuação",
        emoji="⚙️",
        style=discord.ButtonStyle.secondary,
        custom_id="ff:points"
    )
    async def points(self, interaction, button):
        await interaction.response.send_modal(
            PointsModal()
        )

    @discord.ui.button(
        label="Ranking",
        emoji="🏆",
        style=discord.ButtonStyle.secondary,
        custom_id="ff:ranking"
    )
    async def ranking_button(self, interaction, button):
        await interaction.response.send_message(
            embed=ranking(interaction.guild.id),
            ephemeral=True
        )

# =====================================================
# COMANDOS SLASH
# =====================================================

@bot.tree.command(
    name="painel",
    description="Abrir painel administrativo FF Rank"
)
async def painel(interaction: discord.Interaction):
    if not admin(interaction.user):
        return await interaction.response.send_message(
            "Sem permissão.",
            ephemeral=True
        )

    await interaction.response.send_message(
        embed=embed(
            "🏆 FF RANK SYSTEM TREM",
            "Painel administrativo de partidas e rankings."
        ),
        view=Panel(),
        ephemeral=True
    )

@bot.tree.command(
    name="ranking",
    description="Consultar ranking geral"
)
async def ranking_command(
    interaction: discord.Interaction
):
    await interaction.response.send_message(
        embed=ranking(interaction.guild.id)
    )

@bot.tree.command(
    name="temporada",
    description="Consultar ranking da temporada atual"
)
async def temporada_command(
    interaction: discord.Interaction
):
    season = rotate(interaction.guild.id)

    await interaction.response.send_message(
        embed=ranking(
            interaction.guild.id,
            season
        )
    )

@bot.tree.command(
    name="partida",
    description="Consultar uma partida registrada"
)
@app_commands.describe(
    partida_id="Número da partida"
)
async def partida_command(
    interaction: discord.Interaction,
    partida_id: int
):
    match = db.execute("""
        SELECT * FROM matches
        WHERE id=? AND guild=? AND status='confirmed'
    """, (
        partida_id,
        interaction.guild.id
    )).fetchone()

    if not match:
        return await interaction.response.send_message(
            "Partida não encontrada.",
            ephemeral=True
        )

    a = json.loads(match["team_a"])
    b = json.loads(match["team_b"])

    e = embed(
        f"⚔️ PARTIDA #{match['id']}",
        f"**Formato:** {match['mode']}\n"
        f"**Equipe A:** {', '.join(map(str, a))}\n"
        f"**Equipe B:** {', '.join(map(str, b))}\n"
        f"**Vencedor:** {match['winner']}\n"
        f"**MVP:** <@{match['mvp']}>\n"
        f"**Temporada:** #{match['season']}"
    )

    await interaction.response.send_message(
        embed=e,
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
            print(
                f"Erro na temporada de {guild.id}: {error}"
            )

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
        bot.add_view(Panel())

        try:
            await bot.tree.sync()
            synced = True
            print("Comandos slash sincronizados.")
        except Exception as error:
            print(f"Erro ao sincronizar: {error}")

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
async def on_app_command_error(
    interaction,
    error
):
    print(f"Erro de comando: {error}")

    message = "Ocorreu um erro ao executar o comando."

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True
        )

# =====================================================
# EXECUÇÃO
# =====================================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Configure a variável DISCORD_TOKEN."
        )

    bot.run(TOKEN)
