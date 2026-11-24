import os
import re
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands, ui
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "ranking.db")

intents = discord.Intents.default()
intents.members = True  # necessário para buscar nomes/avatars corretamente
bot = commands.Bot(command_prefix="!", intents=intents)

# ─────────────────────────────────────────────
# Banco de dados
# ─────────────────────────────────────────────

def db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS players (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            mvps INTEGER NOT NULL DEFAULT 0,
            matches INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )""")

        con.execute("""CREATE TABLE IF NOT EXISTS season_players (
            guild_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            mvps INTEGER NOT NULL DEFAULT 0,
            matches INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, season, user_id)
        )""")

        con.execute("""CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            winner_ids TEXT NOT NULL,
            loser_ids TEXT NOT NULL,
            mvp_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            registered_by INTEGER NOT NULL,
            season TEXT
        )""")

        con.execute("""CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            ranking_channel_id INTEGER,
            ranking_message_id INTEGER,
            ranking_type TEXT DEFAULT 'lifetime'
        )""")

        # Migração leve: adiciona coluna season se a tabela antiga existir sem ela
        try:
            con.execute("ALTER TABLE matches ADD COLUMN season TEXT")
        except sqlite3.OperationalError:
            pass


def current_season() -> str:
    """Retorna a temporada no formato YYYY-MM (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def ensure_player(con, guild_id, user_id):
    con.execute(
        "INSERT OR IGNORE INTO players (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id)
    )


def ensure_season_player(con, guild_id, season, user_id):
    con.execute(
        "INSERT OR IGNORE INTO season_players (guild_id, season, user_id) VALUES (?, ?, ?)",
        (guild_id, season, user_id)
    )


def get_player(guild_id, user_id):
    with db() as con:
        ensure_player(con, guild_id, user_id)
        return con.execute(
            "SELECT * FROM players WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()


def get_season_player(guild_id, user_id, season=None):
    season = season or current_season()
    with db() as con:
        ensure_season_player(con, guild_id, season, user_id)
        return con.execute(
            "SELECT * FROM season_players WHERE guild_id=? AND season=? AND user_id=?",
            (guild_id, season, user_id)
        ).fetchone()


def parse_mentions(raw, guild):
    ids = [int(x) for x in re.findall(r"<@!?(\d+)>", raw or "")]
    if not ids:
        ids = [int(x) for x in re.findall(r"\b\d{15,22}\b", raw or "")]
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ValueError("Não encontrei jogadores. Use menções do Discord, por exemplo: @Jogador1, @Jogador2.")
    members = []
    for uid in ids:
        member = guild.get_member(uid)
        if member is None:
            raise ValueError(f"Não encontrei o membro <@{uid}> neste servidor. Confira a menção.")
        members.append(member)
    return members


def level_for(xp: int) -> int:
    return max(0, xp // 100)


# ─────────────────────────────────────────────
# Embeds de ranking (visual inspirado na imagem)
# ─────────────────────────────────────────────

def ranking_embed(guild, rows, title_suffix="lifetime", season=None):
    """
    Constrói o embed do ranking no estilo da imagem enviada:
    - TOP 3 com medalhas e destaque
    - Lista limpa com Level + XP
    - Avatar do #1 como thumbnail
    """
    season_label = season or current_season()
    if title_suffix == "monthly":
        title = f"🏆 RANKING MENSAL • {season_label}"
        footer_extra = f"Temporada {season_label}"
    else:
        title = "🏆 RANKING LIFETIME • FREE FIRE"
        footer_extra = "Histórico permanente"

    embed = discord.Embed(
        title=title,
        description="Classificação por XP acumulado • Atualizado automaticamente",
        color=discord.Color.from_rgb(32, 38, 51)  # dark slate como na imagem
    )

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    if not rows:
        embed.description = "Ainda não existem jogadores no ranking. Registre a primeira partida!"
        embed.set_footer(text=f"{guild.name} • FF Rank Bot")
        return embed

    # Thumbnail do líder
    top = rows[0]
    leader = guild.get_member(top["user_id"])
    if leader:
        embed.set_thumbnail(url=leader.display_avatar.url)

    lines = []
    for pos, row in enumerate(rows, start=1):
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"Jogador {row['user_id']}"
        lvl = level_for(row["xp"])
        xp = row["xp"]

        if pos <= 3:
            medal = medals[pos]
            # Destaque especial para o TOP 3
            lines.append(f"{medal} **#{pos}  {name}**\n  Level **{lvl}**  •  **{xp} XP**")
        else:
            lines.append(f"**#{pos}**  {name}\n  Level **{lvl}**  •  **{xp} XP**")

    # Discord tem limite de 4096 caracteres na description
    description = "\n\n".join(lines)
    if len(description) > 4000:
        description = "\n\n".join(lines[:15]) + "\n\n*… e mais*"

    embed.description = description
    embed.set_footer(text=f"{guild.name} • {footer_extra} • FF Rank Bot")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def profile_embed(member, lifetime, season_row, season=None):
    season = season or current_season()
    embed = discord.Embed(
        title=f"🎮 Perfil de {member.display_name}",
        color=discord.Color.from_rgb(32, 38, 51)
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="──────── Lifetime ────────", value="\u200b", inline=False)
    embed.add_field(name="Level", value=str(level_for(lifetime["xp"])), inline=True)
    embed.add_field(name="XP", value=str(lifetime["xp"]), inline=True)
    embed.add_field(name="Partidas", value=str(lifetime["matches"]), inline=True)
    embed.add_field(name="Vitórias", value=str(lifetime["wins"]), inline=True)
    embed.add_field(name="Derrotas", value=str(lifetime["losses"]), inline=True)
    embed.add_field(name="MVPs", value=str(lifetime["mvps"]), inline=True)

    embed.add_field(name=f"──────── Temporada {season} ────────", value="\u200b", inline=False)
    embed.add_field(name="Level", value=str(level_for(season_row["xp"])), inline=True)
    embed.add_field(name="XP", value=str(season_row["xp"]), inline=True)
    embed.add_field(name="Partidas", value=str(season_row["matches"]), inline=True)
    embed.add_field(name="Vitórias", value=str(season_row["wins"]), inline=True)
    embed.add_field(name="Derrotas", value=str(season_row["losses"]), inline=True)
    embed.add_field(name="MVPs", value=str(season_row["mvps"]), inline=True)

    return embed


# ─────────────────────────────────────────────
# Atualização automática do ranking no canal
# ─────────────────────────────────────────────

async def get_or_create_ranking_channel(guild: discord.Guild):
    """Tenta encontrar o canal 🏆・ranking. Se não existir, retorna None."""
    for ch in guild.text_channels:
        if "ranking" in ch.name.lower() and ("🏆" in ch.name or "rank" in ch.name.lower()):
            return ch
    # Fallback: procura exatamente pelo nome
    return discord.utils.get(guild.text_channels, name="🏆・ranking")


async def update_ranking_message(guild: discord.Guild, ranking_type: str = "lifetime"):
    """
    Atualiza (ou cria) a mensagem do TOP 20 no canal de ranking.
    ranking_type: 'lifetime' ou 'monthly'
    """
    with db() as con:
        cfg = con.execute(
            "SELECT * FROM guild_config WHERE guild_id=?", (guild.id,)
        ).fetchone()

    channel = None
    if cfg and cfg["ranking_channel_id"]:
        channel = guild.get_channel(cfg["ranking_channel_id"])

    if channel is None:
        channel = await get_or_create_ranking_channel(guild)

    if channel is None:
        return False  # canal não encontrado

    # Busca os dados
    if ranking_type == "monthly":
        season = current_season()
        with db() as con:
            rows = con.execute(
                """SELECT * FROM season_players
                   WHERE guild_id=? AND season=?
                   ORDER BY xp DESC, wins DESC, mvps DESC
                   LIMIT 20""",
                (guild.id, season)
            ).fetchall()
        embed = ranking_embed(guild, rows, title_suffix="monthly", season=season)
    else:
        with db() as con:
            rows = con.execute(
                """SELECT * FROM players
                   WHERE guild_id=?
                   ORDER BY xp DESC, wins DESC, mvps DESC
                   LIMIT 20""",
                (guild.id,)
            ).fetchall()
        embed = ranking_embed(guild, rows, title_suffix="lifetime")

    message = None
    if cfg and cfg["ranking_message_id"]:
        try:
            message = await channel.fetch_message(cfg["ranking_message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    try:
        if message:
            await message.edit(embed=embed)
            msg_id = message.id
        else:
            # Limpa mensagens antigas do bot no canal (opcional, evita spam)
            async for old in channel.history(limit=15):
                if old.author == bot.user and old.embeds:
                    try:
                        await old.delete()
                    except Exception:
                        pass
            message = await channel.send(embed=embed)
            msg_id = message.id

        # Salva/atualiza config
        with db() as con:
            con.execute(
                """INSERT INTO guild_config (guild_id, ranking_channel_id, ranking_message_id, ranking_type)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       ranking_channel_id=excluded.ranking_channel_id,
                       ranking_message_id=excluded.ranking_message_id,
                       ranking_type=excluded.ranking_type""",
                (guild.id, channel.id, msg_id, ranking_type)
            )
        return True
    except Exception as e:
        print(f"[update_ranking_message] Erro no servidor {guild.id}: {e}")
        return False


# ─────────────────────────────────────────────
# Eventos
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Logado como {bot.user} | {len(synced)} comandos sincronizados")
    except Exception as exc:
        print(f"Erro ao sincronizar comandos: {exc}")

    # Atualiza ranking de todos os servidores que já têm config
    for guild in bot.guilds:
        with db() as con:
            cfg = con.execute(
                "SELECT ranking_type FROM guild_config WHERE guild_id=?", (guild.id,)
            ).fetchone()
        rtype = cfg["ranking_type"] if cfg else "lifetime"
        await update_ranking_message(guild, ranking_type=rtype)


# ─────────────────────────────────────────────
# Comandos de ranking
# ─────────────────────────────────────────────

@bot.tree.command(name="ranking", description="Mostra o ranking individual (TOP 20)")
@app_commands.describe(tipo="Lifetimeetime (histórico permanente) ou monthly (temporada atual)")
@app_commands.choices(tipo=[
    app_commands.Choice(name="Lifetimeetime (histórico)", value="lifetime"),
    app_commands.Choice(name="Mensal (temporada atual)", value="monthly"),
])
async def ranking(interaction: discord.Interaction, tipo: app_commands.Choice[str] = None):
    ranking_type = tipo.value if tipo else "lifetime"
    if ranking_type == "monthly":
        season = current_season()
        with db() as con:
            rows = con.execute(
                """SELECT * FROM season_players
                   WHERE guild_id=? AND season=?
                   ORDER BY xp DESC, wins DESC, mvps DESC LIMIT 20""",
                (interaction.guild_id, season)
            ).fetchall()
        embed = ranking_embed(interaction.guild, rows, title_suffix="monthly", season=season)
    else:
        with db() as con:
            rows = con.execute(
                """SELECT * FROM players
                   WHERE guild_id=?
                   ORDER BY xp DESC, wins DESC, mvps DESC LIMIT 20""",
                (interaction.guild_id,)
            ).fetchall()
        embed = ranking_embed(interaction.guild, rows, title_suffix="lifetime")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setup_ranking", description="Configura o canal de ranking automático (admin)")
@app_commands.describe(
    canal="Canal onde o ranking será publicado (deixe vazio para procurar 🏆・ranking)",
    tipo="Tipo de ranking que será exibido no canal"
)
@app_commands.choices(tipo=[
    app_commands.Choice(name="Lifetimeetime (histórico permanente)", value="lifetime"),
    app_commands.Choice(name="Mensal (temporada atual)", value="monthly"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_ranking(
    interaction: discord.Interaction,
    canal: discord.TextChannel = None,
    tipo: app_commands.Choice[str] = None
):
    await interaction.response.defer(ephemeral=True)
    ranking_type = tipo.value if tipo else "lifetime"

    if canal is None:
        canal = await get_or_create_ranking_channel(interaction.guild)
        if canal is None:
            return await interaction.followup.send(
                "❌ Não encontrei o canal `🏆・ranking`.\n"
                "Crie um canal com esse nome ou informe o canal no parâmetro `canal`.",
                ephemeral=True
            )

    # Salva o canal e força a publicação
    with db() as con:
        con.execute(
            """INSERT INTO guild_config (guild_id, ranking_channel_id, ranking_type)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   ranking_channel_id=excluded.ranking_channel_id,
                   ranking_type=excluded.ranking_type""",
            (interaction.guild_id, canal.id, ranking_type)
        )

    ok = await update_ranking_message(interaction.guild, ranking_type=ranking_type)
    if ok:
        await interaction.followup.send(
            f"✅ Ranking configurado em {canal.mention}!\n"
            f"Tipo: **{ranking_type}**\n"
            f"A mensagem será atualizada automaticamente sempre que uma partida for registrada.",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"⚠️ Canal {canal.mention} encontrado, mas não consegui enviar/editar a mensagem.\n"
            "Verifique se o bot tem permissão de **Enviar Mensagens** e **Gerenciar Mensagens** no canal.",
            ephemeral=True
        )


@bot.tree.command(name="atualizar_ranking", description="Força a atualização da mensagem de ranking no canal (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def atualizar_ranking(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with db() as con:
        cfg = con.execute(
            "SELECT ranking_type FROM guild_config WHERE guild_id=?", (interaction.guild_id,)
        ).fetchone()
    rtype = cfg["ranking_type"] if cfg else "lifetime"
    ok = await update_ranking_message(interaction.guild, ranking_type=rtype)
    if ok:
        await interaction.followup.send("✅ Ranking atualizado com sucesso!", ephemeral=True)
    else:
        await interaction.followup.send(
            "❌ Não foi possível atualizar. Execute `/setup_ranking` primeiro ou verifique as permissões do bot no canal.",
            ephemeral=True
        )


# ─────────────────────────────────────────────
# Registro de partida + atualização automática
# ─────────────────────────────────────────────

@bot.tree.command(name="registrar_partida", description="Registra vencedores, perdedores e MVP de uma partida")
@app_commands.describe(
    vencedores="Mencione todos os jogadores do time vencedor, separados por espaço ou vírgula",
    perdedores="Mencione todos os jogadores do time perdedor, separados por espaço ou vírgula",
    mvp="Escolha o MVP entre os jogadores do time vencedor"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def registrar_partida(
    interaction: discord.Interaction,
    vencedores: str,
    perdedores: str,
    mvp: discord.Member
):
    try:
        winners = parse_mentions(vencedores, interaction.guild)
        losers = parse_mentions(perdedores, interaction.guild)
        winner_ids = {m.id for m in winners}
        loser_ids = {m.id for m in losers}

        if winner_ids & loser_ids:
            raise ValueError("Um jogador não pode estar nos dois times.")
        if mvp.id not in winner_ids:
            raise ValueError("O MVP precisa fazer parte do time vencedor.")
        if len(winner_ids) < 1 or len(loser_ids) < 1:
            raise ValueError("Informe pelo menos um jogador em cada time.")

        season = current_season()
        now = datetime.now(timezone.utc).isoformat()

        with db() as con:
            # ── Lifetime ──
            for member in winners:
                ensure_player(con, interaction.guild_id, member.id)
                con.execute(
                    """UPDATE players SET xp=xp+17, wins=wins+1, matches=matches+1
                       WHERE guild_id=? AND user_id=?""",
                    (interaction.guild_id, member.id)
                )
            for member in losers:
                ensure_player(con, interaction.guild_id, member.id)
                con.execute(
                    """UPDATE players SET xp=MAX(0, xp-3), losses=losses+1, matches=matches+1
                       WHERE guild_id=? AND user_id=?""",
                    (interaction.guild_id, member.id)
                )
            con.execute(
                "UPDATE players SET xp=xp+10, mvps=mvps+1 WHERE guild_id=? AND user_id=?",
                (interaction.guild_id, mvp.id)
            )

            # ── Temporada atual ──
            for member in winners:
                ensure_season_player(con, interaction.guild_id, season, member.id)
                con.execute(
                    """UPDATE season_players SET xp=xp+17, wins=wins+1, matches=matches+1
                       WHERE guild_id=? AND season=? AND user_id=?""",
                    (interaction.guild_id, season, member.id)
                )
            for member in losers:
                ensure_season_player(con, interaction.guild_id, season, member.id)
                con.execute(
                    """UPDATE season_players SET xp=MAX(0, xp-3), losses=losses+1, matches=matches+1
                       WHERE guild_id=? AND season=? AND user_id=?""",
                    (interaction.guild_id, season, member.id)
                )
            con.execute(
                """UPDATE season_players SET xp=xp+10, mvps=mvps+1
                   WHERE guild_id=? AND season=? AND user_id=?""",
                (interaction.guild_id, season, mvp.id)
            )

            # ── Histórico ──
            con.execute(
                """INSERT INTO matches (guild_id, winner_ids, loser_ids, mvp_id, created_at, registered_by, season)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    interaction.guild_id,
                    ",".join(map(str, sorted(winner_ids))),
                    ",".join(map(str, sorted(loser_ids))),
                    mvp.id,
                    now,
                    interaction.user.id,
                    season
                )
            )

        embed = discord.Embed(title="✅ Partida registrada!", color=discord.Color.green())
        embed.add_field(name="🏆 Time vencedor", value=", ".join(m.mention for m in winners), inline=False)
        embed.add_field(name="❌ Time perdedor", value=", ".join(m.mention for m in losers), inline=False)
        embed.add_field(name="⭐ MVP", value=mvp.mention, inline=True)
        embed.add_field(
            name="Pontuação",
            value="Vencedores: **+17 XP** · MVP: **+10 XP** extra\nPerdedores: **-3 XP** líquidos (mínimo 0)",
            inline=False
        )
        embed.set_footer(text=f"Temporada {season}")
        await interaction.response.send_message(embed=embed)

        # Atualiza o ranking automaticamente
        with db() as con:
            cfg = con.execute(
                "SELECT ranking_type FROM guild_config WHERE guild_id=?", (interaction.guild_id,)
            ).fetchone()
        rtype = cfg["ranking_type"] if cfg else "lifetime"
        await update_ranking_message(interaction.guild, ranking_type=rtype)

    except ValueError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


# ─────────────────────────────────────────────
# Perfil completo (Lifetimeetime + Temporada)
# ─────────────────────────────────────────────

@bot.tree.command(name="perfil", description="Mostra o perfil completo (lifetime + temporada atual)")
@app_commands.describe(jogador="Jogador que deseja consultar (opcional)")
async def perfil(interaction: discord.Interaction, jogador: discord.Member = None):
    member = jogador or interaction.user
    lifetime = get_player(interaction.guild_id, member.id)
    season_row = get_season_player(interaction.guild_id, member.id)
    embed = profile_embed(member, lifetime, season_row)
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
# Histórico
# ─────────────────────────────────────────────

@bot.tree.command(name="historico", description="Mostra as últimas partidas registradas")
async def historico(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM matches WHERE guild_id=? ORDER BY id DESC LIMIT 10",
            (interaction.guild_id,)
        ).fetchall()
    if not rows:
        return await interaction.response.send_message("Ainda não há partidas registradas.")
    lines = []
    for row in rows:
        winners = [f"<@{x}>" for x in row["winner_ids"].split(",") if x]
        losers = [f"<@{x}>" for x in row["loser_ids"].split(",") if x]
        season = row["season"] or "?"
        lines.append(
            f"**Partida #{row['id']}** ({season})\n"
            f"🏆 {', '.join(winners)}\n"
            f"❌ {', '.join(losers)}\n"
            f"⭐ <@{row['mvp_id']}>"
        )
    embed = discord.Embed(
        title="📜 Últimas partidas",
        description="\n\n".join(lines),
        color=discord.Color.from_rgb(32, 38, 51)
    )
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
# Ajuste manual de XP
# ─────────────────────────────────────────────

@bot.tree.command(name="ajustar_xp", description="Adiciona ou remove XP de um jogador (admin)")
@app_commands.describe(
    jogador="Jogador",
    quantidade="XP positivo para adicionar ou negativo para remover",
    escopo="Aplicar no lifetime, na temporada atual ou em ambos"
)
@app_commands.choices(escopo=[
    app_commands.Choice(name="Lifetimeetime + Temporada", value="both"),
    app_commands.Choice(name="Apenas Lifetime", value="lifetime"),
    app_commands.Choice(name="Apenas Temporada atual", value="monthly"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def ajustar_xp(
    interaction: discord.Interaction,
    jogador: discord.Member,
    quantidade: int,
    escopo: app_commands.Choice[str] = None
):
    scope = escopo.value if escopo else "both"
    season = current_season()
    messages = []

    with db() as con:
        if scope in ("both", "lifetime"):
            ensure_player(con, interaction.guild_id, jogador.id)
            con.execute(
                "UPDATE players SET xp=MAX(0, xp+?) WHERE guild_id=? AND user_id=?",
                (quantidade, interaction.guild_id, jogador.id)
            )
            row = con.execute(
                "SELECT xp FROM players WHERE guild_id=? AND user_id=?",
                (interaction.guild_id, jogador.id)
            ).fetchone()
            messages.append(f"Lifetimeetime: **{row['xp']} XP**")

        if scope in ("both", "monthly"):
            ensure_season_player(con, interaction.guild_id, season, jogador.id)
            con.execute(
                "UPDATE season_players SET xp=MAX(0, xp+?) WHERE guild_id=? AND season=? AND user_id=?",
                (quantidade, interaction.guild_id, season, jogador.id)
            )
            row = con.execute(
                "SELECT xp FROM season_players WHERE guild_id=? AND season=? AND user_id=?",
                (interaction.guild_id, season, jogador.id)
            ).fetchone()
            messages.append(f"Temporada {season}: **{row['xp']} XP**")

    await interaction.response.send_message(
        f"✅ XP atualizado para {jogador.mention}:\n" + "\n".join(messages)
    )

    # Atualiza ranking
    with db() as con:
        cfg = con.execute(
            "SELECT ranking_type FROM guild_config WHERE guild_id=?", (interaction.guild_id,)
        ).fetchone()
    rtype = cfg["ranking_type"] if cfg else "lifetime"
    await update_ranking_message(interaction.guild, ranking_type=rtype)


# ─────────────────────────────────────────────
# Reset
# ─────────────────────────────────────────────

@bot.tree.command(name="resetar_ranking_confirmar", description="Zera XP, estatísticas e histórico (administrador)")
@app_commands.describe(
    confirmacao="Digite RESETAR para confirmar",
    escopo="O que deseja resetar"
)
@app_commands.choices(escopo=[
    app_commands.Choice(name="Tudo (lifetime + temporadas + histórico)", value="all"),
    app_commands.Choice(name="Apenas temporada atual", value="season"),
    app_commands.Choice(name="Apenas lifetime + histórico", value="lifetime"),
])
@app_commands.checks.has_permissions(administrator=True)
async def resetar_ranking_confirmar(
    interaction: discord.Interaction,
    confirmacao: str,
    escopo: app_commands.Choice[str] = None
):
    if confirmacao != "RESETAR":
        return await interaction.response.send_message(
            "Digite exatamente **RESETAR** para confirmar.", ephemeral=True
        )

    scope = escopo.value if escopo else "all"
    season = current_season()

    with db() as con:
        if scope in ("all", "lifetime"):
            con.execute("DELETE FROM players WHERE guild_id=?", (interaction.guild_id,))
            con.execute("DELETE FROM matches WHERE guild_id=?", (interaction.guild_id,))
        if scope in ("all", "season"):
            con.execute(
                "DELETE FROM season_players WHERE guild_id=? AND season=?",
                (interaction.guild_id, season)
            )
        if scope == "all":
            con.execute("DELETE FROM season_players WHERE guild_id=?", (interaction.guild_id,))

    await interaction.response.send_message(
        f"🧹 Reset concluído (`{scope}`)."
    )

    with db() as con:
        cfg = con.execute(
            "SELECT ranking_type FROM guild_config WHERE guild_id=?", (interaction.guild_id,)
        ).fetchone()
    rtype = cfg["ranking_type"] if cfg else "lifetime"
    await update_ranking_message(interaction.guild, ranking_type=rtype)


# ─────────────────────────────────────────────
# Painel de administração (View com botões)
# ─────────────────────────────────────────────

class AdminPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistente

    @ui.button(label="Atualizar Ranking", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="admin_update_ranking")
    async def update_ranking_btn(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "Você precisa de permissão **Gerenciar servidor**.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        with db() as con:
            cfg = con.execute(
                "SELECT ranking_type FROM guild_config WHERE guild_id=?", (interaction.guild_id,)
            ).fetchone()
        rtype = cfg["ranking_type"] if cfg else "lifetime"
        ok = await update_ranking_message(interaction.guild, ranking_type=rtype)
        if ok:
            await interaction.followup.send("✅ Ranking atualizado!", ephemeral=True)
        else:
            await interaction.followup.send(
                "❌ Falha ao atualizar. Use `/setup_ranking` primeiro.", ephemeral=True
            )

    @ui.button(label="Ver Ranking Lifetime", style=discord.ButtonStyle.secondary, emoji="🏆", custom_id="admin_show_lifetime")
    async def show_lifetime(self, interaction: discord.Interaction, button: ui.Button):
        with db() as con:
            rows = con.execute(
                """SELECT * FROM players WHERE guild_id=?
                   ORDER BY xp DESC, wins DESC, mvps DESC LIMIT 20""",
                (interaction.guild_id,)
            ).fetchall()
        embed = ranking_embed(interaction.guild, rows, title_suffix="lifetime")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="Ver Ranking Mensal", style=discord.ButtonStyle.secondary, emoji="📅", custom_id="admin_show_monthly")
    async def show_monthly(self, interaction: discord.Interaction, button: ui.Button):
        season = current_season()
        with db() as con:
            rows = con.execute(
                """SELECT * FROM season_players
                   WHERE guild_id=? AND season=?
                   ORDER BY xp DESC, wins DESC, mvps DESC LIMIT 20""",
                (interaction.guild_id, season)
            ).fetchall()
        embed = ranking_embed(interaction.guild, rows, title_suffix="monthly", season=season)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="painel_admin", description="Abre o painel de administração do ranking")
@app_commands.checks.has_permissions(manage_guild=True)
async def painel_admin(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚙️ Painel de Administração • FF Rank Bot",
        description=(
            "**Comandos rápidos:**\n"
            "• `/registrar_partida` — registra uma partida (atualiza o ranking automaticamente)\n"
            "• `/setup_ranking` — configura o canal `🏆・ranking`\n"
            "• `/atualizar_ranking` — força atualização da mensagem\n"
            "• `/ajustar_xp` — ajuste manual de XP\n"
            "• `/resetar_ranking_confirmar` — reset (com confirmação)\n\n"
            "**Botões abaixo** para ações rápidas."
        ),
        color=discord.Color.from_rgb(32, 38, 51)
    )
    embed.set_footer(text="Apenas administradores / Gerenciar servidor")
    await interaction.response.send_message(embed=embed, view=AdminPanelView(), ephemeral=True)


# ─────────────────────────────────────────────
# Tratamento de erros
# ─────────────────────────────────────────────

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Você precisa ter permissão de **Gerenciar servidor** para usar esse comando."
    else:
        msg = f"Ocorreu um erro: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ─────────────────────────────────────────────
# Inicialização
# ─────────────────────────────────────────────

if not TOKEN:
    raise RuntimeError("Defina DISCORD_TOKEN nas variáveis de ambiente.")

# View persistente para o painel (sobrevive a restarts)
bot.add_view(AdminPanelView())

bot.run(TOKEN)
