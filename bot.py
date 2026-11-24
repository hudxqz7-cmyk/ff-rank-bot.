import os
import re
import io
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "ranking.db")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

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
        con.execute("""CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            winner_ids TEXT NOT NULL,
            loser_ids TEXT NOT NULL,
            mvp_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            registered_by INTEGER NOT NULL
        )""")

def ensure_player(con, guild_id, user_id):
    con.execute(
        "INSERT OR IGNORE INTO players (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id)
    )

def get_player(guild_id, user_id):
    with db() as con:
        ensure_player(con, guild_id, user_id)
        return con.execute(
            "SELECT * FROM players WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()

def parse_mentions(raw, guild):
    ids = [int(x) for x in re.findall(r"<@!?(\d+)>", raw or "")]
    # Also accept numeric Discord IDs separated by commas/spaces.
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

def level_for(xp):
    return max(0, xp // 100)

def ranking_embed(guild, rows):
    embed = discord.Embed(
        title="🏆 RANKING INDIVIDUAL • FREE FIRE",
        description="Classificação por XP acumulado",
        color=discord.Color.from_rgb(32, 38, 51)
    )
    medals = ["🥇", "🥈", "🥉"]
    if not rows:
        embed.description = "Ainda não existem jogadores no ranking. Registre a primeira partida!"
    for pos, row in enumerate(rows, start=1):
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"Jogador {row['user_id']}"
        medal = medals[pos-1] if pos <= 3 else f"**#{pos}**"
        embed.add_field(
            name=f"{medal} • {name}",
            value=f"Level **{level_for(row['xp'])}**  ·  XP **{row['xp']}**",
            inline=False
        )
    embed.set_footer(text=f"{guild.name} • FF Rank Bot")
    return embed

@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Logado como {bot.user} | {len(synced)} comandos sincronizados")
    except Exception as exc:
        print(f"Erro ao sincronizar comandos: {exc}")

@bot.tree.command(name="ranking", description="Mostra o ranking individual do servidor")
async def ranking(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM players WHERE guild_id=? ORDER BY xp DESC, wins DESC, mvps DESC",
            (interaction.guild_id,)
        ).fetchall()
    await interaction.response.send_message(embed=ranking_embed(interaction.guild, rows[:20]))

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

        with db() as con:
            for member in winners:
                ensure_player(con, interaction.guild_id, member.id)
                con.execute(
                    """UPDATE players SET xp=xp+17, wins=wins+1, matches=matches+1
                       WHERE guild_id=? AND user_id=?""",
                    (interaction.guild_id, member.id)
                )
            for member in losers:
                ensure_player(con, interaction.guild_id, member.id)
                # +2 participation and -5 defeat, with a zero XP floor.
                con.execute(
                    """UPDATE players SET xp=MAX(0, xp-3), losses=losses+1, matches=matches+1
                       WHERE guild_id=? AND user_id=?""",
                    (interaction.guild_id, member.id)
                )
            con.execute(
                "UPDATE players SET xp=xp+10, mvps=mvps+1 WHERE guild_id=? AND user_id=?",
                (interaction.guild_id, mvp.id)
            )
            con.execute(
                """INSERT INTO matches (guild_id, winner_ids, loser_ids, mvp_id, created_at, registered_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    interaction.guild_id,
                    ",".join(map(str, sorted(winner_ids))),
                    ",".join(map(str, sorted(loser_ids))),
                    mvp.id,
                    datetime.now(timezone.utc).isoformat(),
                    interaction.user.id
                )
            )

        embed = discord.Embed(title="✅ Partida registrada!", color=discord.Color.green())
        embed.add_field(name="🏆 Time vencedor", value=", ".join(m.mention for m in winners), inline=False)
        embed.add_field(name="❌ Time perdedor", value=", ".join(m.mention for m in losers), inline=False)
        embed.add_field(name="⭐ MVP", value=mvp.mention, inline=True)
        embed.add_field(name="Pontuação", value="Vencedores: +17 XP · MVP: +10 XP extra\nPerdedores: -3 XP líquidos (mínimo 0)", inline=False)
        await interaction.response.send_message(embed=embed)
    except ValueError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)

@bot.tree.command(name="perfil", description="Mostra o perfil e as estatísticas de um jogador")
@app_commands.describe(jogador="Jogador que deseja consultar (opcional)")
async def perfil(interaction: discord.Interaction, jogador: discord.Member = None):
    member = jogador or interaction.user
    row = get_player(interaction.guild_id, member.id)
    embed = discord.Embed(title=f"🎮 Perfil de {member.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(level_for(row["xp"])), inline=True)
    embed.add_field(name="XP", value=str(row["xp"]), inline=True)
    embed.add_field(name="Partidas", value=str(row["matches"]), inline=True)
    embed.add_field(name="Vitórias", value=str(row["wins"]), inline=True)
    embed.add_field(name="Derrotas", value=str(row["losses"]), inline=True)
    embed.add_field(name="MVPs", value=str(row["mvps"]), inline=True)
    await interaction.response.send_message(embed=embed)

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
        lines.append(f"**Partida #{row['id']}** — 🏆 {', '.join(winners)} | ❌ {', '.join(losers)} | ⭐ <@{row['mvp_id']}>")
    embed = discord.Embed(title="📜 Últimas partidas", description="\n\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ajustar_xp", description="Adiciona ou remove XP de um jogador (admin)")
@app_commands.describe(jogador="Jogador", quantidade="XP positivo para adicionar ou negativo para remover")
@app_commands.checks.has_permissions(manage_guild=True)
async def ajustar_xp(interaction: discord.Interaction, jogador: discord.Member, quantidade: int):
    with db() as con:
        ensure_player(con, interaction.guild_id, jogador.id)
        con.execute(
            "UPDATE players SET xp=MAX(0, xp+?) WHERE guild_id=? AND user_id=?",
            (quantidade, interaction.guild_id, jogador.id)
        )
        row = con.execute(
            "SELECT xp FROM players WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, jogador.id)
        ).fetchone()
    await interaction.response.send_message(f"✅ XP atualizado para {jogador.mention}: **{row['xp']} XP**.")

# The reset command requires an explicit confirmation phrase to reduce accidental resets.
@bot.tree.command(name="resetar_ranking_confirmar", description="Zera XP, estatísticas e histórico (administrador)")
@app_commands.describe(confirmacao="Digite RESETAR para confirmar")
@app_commands.checks.has_permissions(administrator=True)
async def resetar_ranking_confirmar(interaction: discord.Interaction, confirmacao: str):
    if confirmacao != "RESETAR":
        return await interaction.response.send_message("Digite exatamente RESETAR para confirmar.", ephemeral=True)
    with db() as con:
        con.execute("DELETE FROM players WHERE guild_id=?", (interaction.guild_id,))
        con.execute("DELETE FROM matches WHERE guild_id=?", (interaction.guild_id,))
    await interaction.response.send_message("🧹 Ranking e histórico deste servidor foram zerados.")

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

if not TOKEN:
    raise RuntimeError("Defina DISCORD_TOKEN nas variáveis de ambiente.")
bot.run(TOKEN)
