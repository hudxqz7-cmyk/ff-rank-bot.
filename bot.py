import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
ADMIN_ROLE_ID = 1574625648868327530
GENERAL_CHANNEL_ID = 1574568353350291496
SEASON_CHANNEL_ID = 1574624786045210724
ANNOUNCEMENT_CHANNEL_ID = 1574868120164765807
SEASON_DAYS = 15
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/ff_rank.sqlite")
BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")
WIN_XP = int(os.getenv("WIN_XP", "20"))
LOSS_XP = int(os.getenv("LOSS_XP", "5"))
MVP_XP = int(os.getenv("MVP_XP", "10"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATABASE_PATH)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS players(guild_id INTEGER,user_id INTEGER,xp INTEGER DEFAULT 0,wins INTEGER DEFAULT 0,losses INTEGER DEFAULT 0,mvps INTEGER DEFAULT 0,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS seasons(guild_id INTEGER PRIMARY KEY,season_no INTEGER,started_at TEXT);
        CREATE TABLE IF NOT EXISTS matches(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,season_no INTEGER,created_at TEXT,registered_by INTEGER,winners TEXT,losers TEXT,mvp INTEGER);
        CREATE TABLE IF NOT EXISTS xp_adjustments(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,user_id INTEGER,amount INTEGER,reason TEXT,admin_id INTEGER,created_at TEXT);
        CREATE TABLE IF NOT EXISTS panels(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,message_id INTEGER);
        """)

def season(gid):
    with db() as c:
        r = c.execute("SELECT * FROM seasons WHERE guild_id=?", (gid,)).fetchone()
        if not r:
            c.execute("INSERT INTO seasons VALUES(?,?,?)", (gid,1,now()))
            return 1
        started = datetime.fromisoformat(r["started_at"])
        if datetime.now(timezone.utc) - started >= timedelta(days=SEASON_DAYS):
            n = r["season_no"] + 1
            c.execute("UPDATE seasons SET season_no=?,started_at=? WHERE guild_id=?", (n,now(),gid))
            return n
        return r["season_no"]

def ensure_player(c,gid,uid):
    c.execute("INSERT OR IGNORE INTO players(guild_id,user_id) VALUES(?,?)",(gid,uid))

def save_match(gid,author,winners,losers,mvp):
    sn = season(gid)
    with db() as c:
        cur = c.execute("INSERT INTO matches(guild_id,season_no,created_at,registered_by,winners,losers,mvp) VALUES(?,?,?,?,?,?,?)",
            (gid,sn,now(),author,",".join(map(str,winners)),",".join(map(str,losers)),mvp))
        mid = cur.lastrowid
        for uid in winners:
            ensure_player(c,gid,uid)
            c.execute("UPDATE players SET xp=xp+?,wins=wins+1 WHERE guild_id=? AND user_id=?",(WIN_XP,gid,uid))
        for uid in losers:
            ensure_player(c,gid,uid)
            c.execute("UPDATE players SET xp=xp+?,losses=losses+1 WHERE guild_id=? AND user_id=?",(LOSS_XP,gid,uid))
        if mvp:
            ensure_player(c,gid,mvp)
            c.execute("UPDATE players SET xp=xp+?,mvps=mvps+1 WHERE guild_id=? AND user_id=?",(MVP_XP,gid,mvp))
    return mid

def admin(member):
    return member.guild_permissions.administrator or any(r.id == ADMIN_ROLE_ID for r in getattr(member,"roles",[]))

def profile(gid,uid):
    with db() as c:
        r = c.execute("SELECT * FROM players WHERE guild_id=? AND user_id=?",(gid,uid)).fetchone()
        return dict(r) if r else {"xp":0,"wins":0,"losses":0,"mvps":0}

def ranking(gid,guild):
    sn = season(gid)
    with db() as c:
        matches = c.execute("SELECT winners,losers,mvp FROM matches WHERE guild_id=? AND season_no=?",(gid,sn)).fetchall()
    stats = {}
    for m in matches:
        for uid in filter(None,m["winners"].split(",")):
            s=stats.setdefault(int(uid),[0,0,0]); s[0]+=1
        for uid in filter(None,m["losers"].split(",")):
            s=stats.setdefault(int(uid),[0,0,0]); s[1]+=1
        if m["mvp"]:
            s=stats.setdefault(m["mvp"],[0,0,0]); s[2]+=1
    rows=[]
    for uid,(w,l,v) in stats.items():
        rows.append((uid,w*WIN_XP+l*LOSS_XP+v*MVP_XP,w,l,v))
    rows.sort(key=lambda x:(x[1],x[2],x[4]),reverse=True)
    e=discord.Embed(title=f"🏆 Ranking — Temporada {sn}",color=discord.Color.gold())
    e.description="\n".join(f"**{i}. {(guild.get_member(x[0]).display_name if guild.get_member(x[0]) else '<@'+str(x[0])+'>')}** — `{x[1]} XP` · {x[2]}V/{x[3]}D · MVP {x[4]}" for i,x in enumerate(rows[:10],1)) or "Nenhuma partida registrada nesta temporada."
    return e

def backup_db():
    Path(BACKUP_DIR).mkdir(parents=True,exist_ok=True)
    dest=str(Path(BACKUP_DIR)/f"ff_rank_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite")
    with db() as src:
        target=sqlite3.connect(dest); src.backup(target); target.close()
    return dest

class MatchView(discord.ui.View):
    def __init__(self,gid,owner):
        super().__init__(timeout=300)
        self.gid,self.owner=gid,owner
        self.winners=[]; self.losers=[]; self.mvp=None
    async def interaction_check(self,i):
        if i.user.id != self.owner:
            await i.response.send_message("Somente quem abriu o registro pode confirmar.",ephemeral=True)
            return False
        return True
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Selecione os VENCEDORES", min_values=1, max_values=5, row=0)
    async def win_select(self,i:discord.Interaction,select:discord.ui.UserSelect):
        self.winners=[u.id for u in select.values]; await i.response.defer()
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Selecione os PERDEDORES", min_values=1, max_values=5, row=1)
    async def lose_select(self,i:discord.Interaction,select:discord.ui.UserSelect):
        self.losers=[u.id for u in select.values]; await i.response.defer()
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Selecione o MVP (opcional)", min_values=1, max_values=1, row=2)
    async def mvp_select(self,i:discord.Interaction,select:discord.ui.UserSelect):
        self.mvp=select.values[0].id; await i.response.defer()
    @discord.ui.button(label="Confirmar registro",style=discord.ButtonStyle.success,row=3)
    async def confirm(self,i:discord.Interaction,button:discord.ui.Button):
        if not self.winners or not self.losers:
            return await i.response.send_message("Selecione vencedores e perdedores.",ephemeral=True)
        if set(self.winners)&set(self.losers):
            return await i.response.send_message("Um jogador não pode estar nos dois times.",ephemeral=True)
        if self.mvp and self.mvp not in self.winners+self.losers:
            return await i.response.send_message("O MVP precisa participar da partida.",ephemeral=True)
        mid=save_match(self.gid,i.user.id,self.winners,self.losers,self.mvp)
        for item in self.children: item.disabled=True
        await i.response.edit_message(content=f"✅ Partida #{mid} registrada! XP atualizado.",view=self)
        ch=i.guild.get_channel(GENERAL_CHANNEL_ID)
        if ch:
            await ch.send(f"⚔️ Partida #{mid} | Vencedores: {', '.join('<@%s>'%u for u in self.winners)} | Perd edores: {', '.join('<@%s>'%u for u in self.losers)}"+(f" | MVP: <@{self.mvp}>" if self.mvp else ""))

class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Registrar partida",emoji="⚔️",style=discord.ButtonStyle.success,custom_id="ffrank:register")
    async def register(self,i,b):
        await i.response.send_message("Selecione os jogadores nas listas e confirme:",view=MatchView(i.guild_id,i.user.id),ephemeral=True)
    @discord.ui.button(label="Ranking",emoji="🏆",style=discord.ButtonStyle.primary,custom_id="ffrank:ranking")
    async def rank(self,i,b):
        await i.response.send_message(embed=ranking(i.guild_id,i.guild),ephemeral=True)
    @discord.ui.button(label="Meu perfil",emoji="👤",style=discord.ButtonStyle.secondary,custom_id="ffrank:profile")
    async def prof(self,i,b):
        d=profile(i.guild_id,i.user.id)
        e=discord.Embed(title=f"Perfil de {i.user.display_name}",color=discord.Color.blurple())
        for k,v in [("XP",d["xp"]),("Vitórias",d["wins"]),("Derrotas",d["losses"]),("MVPs",d["mvps"])]: e.add_field(name=k,value=str(v))
        await i.response.send_message(embed=e,ephemeral=True)
    @discord.ui.button(label="Backup",emoji="💾",style=discord.ButtonStyle.secondary,custom_id="ffrank:backup")
    async def backup(self,i,b):
        if not admin(i.user): return await i.response.send_message("Apenas admins.",ephemeral=True)
        p=backup_db(); await i.response.send_message(f"Backup criado: `{Path(p).name}`",ephemeral=True)
    @discord.ui.button(label="Pontuação",emoji="📊",style=discord.ButtonStyle.secondary,custom_id="ffrank:points")
    async def points(self,i,b):
        await i.response.send_message(f"Vitória: {WIN_XP} XP · Derrota: {LOSS_XP} XP · MVP: {MVP_XP} XP",ephemeral=True)

@bot.event
async def on_ready():
    init_db(); bot.add_view(Panel())
    try: await bot.tree.sync()
    except Exception as e: print("Erro ao sincronizar comandos:",e)
    print(f"FF RANK SYSTEM TREM online: {bot.user}")

@bot.tree.command(name="painel",description="Mostra o painel do FF Rank")
async def painel(i:discord.Interaction):
    await i.response.send_message("🎮 **FF RANK SYSTEM TREM**",view=Panel(),ephemeral=True)

@bot.tree.command(name="painel_fixo",description="Publica o painel permanente em um canal")
async def painel_fixo(i:discord.Interaction,canal:discord.TextChannel):
    if not admin(i.user): return await i.response.send_message("Sem permissão.",ephemeral=True)
    e=discord.Embed(title="🎮 FF RANK SYSTEM TREM",description="Registre partidas, veja o ranking e consulte seu perfil.",color=discord.Color.blurple())
    msg=await canal.send(embed=e,view=Panel())
    with db() as c: c.execute("INSERT OR REPLACE INTO panels VALUES(?,?,?)",(i.guild_id,canal.id,msg.id))
    await i.response.send_message(f"Painel publicado em {canal.mention}.",ephemeral=True)

@bot.tree.command(name="ranking",description="Mostra o ranking atual")
async def ranking_cmd(i:discord.Interaction):
    await i.response.send_message(embed=ranking(i.guild_id,i.guild))

@bot.tree.command(name="perfil",description="Consulta o perfil de um jogador")
async def perfil(i:discord.Interaction,membro:discord.Member=None):
    m=membro or i.user; d=profile(i.guild_id,m.id)
    e=discord.Embed(title=f"Perfil de {m.display_name}",color=discord.Color.blurple())
    for k,v in [("XP",d["xp"]),("Vitórias",d["wins"]),("Derrotas",d["losses"]),("MVPs",d["mvps"])]: e.add_field(name=k,value=str(v))
    await i.response.send_message(embed=e)

async def change_xp(i,membro,quantidade,motivo,delta):
    if not admin(i.user): return await i.response.send_message("Sem permissão.",ephemeral=True)
    amount=quantidade*delta
    with db() as c:
        ensure_player(c,i.guild_id,membro.id)
        c.execute("UPDATE players SET xp=MAX(0,xp+?) WHERE guild_id=? AND user_id=?",(amount,i.guild_id,membro.id))
        c.execute("INSERT INTO xp_adjustments(guild_id,user_id,amount,reason,admin_id,created_at) VALUES(?,?,?,?,?,?)",(i.guild_id,membro.id,amount,motivo,i.user.id,now()))
    await i.response.send_message(f"XP ajustado para {membro.mention}: {amount:+} ({motivo}).")

@bot.tree.command(name="addxp",description="Adiciona XP (admin)")
async def addxp(i:discord.Interaction,membro:discord.Member,quantidade:app_commands.Range[int,1,100000],motivo:str="Ajuste administrativo"):
    await change_xp(i,membro,quantidade,motivo,1)

@bot.tree.command(name="removexp",description="Remove XP (admin)")
async def removexp(i:discord.Interaction,membro:discord.Member,quantidade:app_commands.Range[int,1,100000],motivo:str="Ajuste administrativo"):
    await change_xp(i,membro,quantidade,motivo,-1)

@bot.tree.command(name="setxp",description="Define o XP (admin)")
async def setxp(i:discord.Interaction,membro:discord.Member,quantidade:app_commands.Range[int,0,1000000],motivo:str="Ajuste administrativo"):
    if not admin(i.user): return await i.response.send_message("Sem permissão.",ephemeral=True)
    old=profile(i.guild_id,membro.id)["xp"]
    with db() as c:
        ensure_player(c,i.guild_id,membro.id)
        c.execute("UPDATE players SET xp=? WHERE guild_id=? AND user_id=?",(quantidade,i.guild_id,membro.id))
        c.execute("INSERT INTO xp_adjustments(guild_id,user_id,amount,reason,admin_id,created_at) VALUES(?,?,?,?,?,?)",(i.guild_id,membro.id,quantidade-old,motivo,i.user.id,now()))
    await i.response.send_message(f"XP de {membro.mention} definido para {quantidade}.")

@bot.tree.command(name="backup",description="Cria backup do banco (admin)")
async def backup_cmd(i:discord.Interaction):
    if not admin(i.user): return await i.response.send_message("Sem permissão.",ephemeral=True)
    p=backup_db(); await i.response.send_message(f"Backup criado: `{Path(p).name}`",ephemeral=True)

@bot.tree.command(name="temporada",description="Mostra a temporada atual")
async def temporada_cmd(i:discord.Interaction):
    sn=season(i.guild_id)
    with db() as c: r=c.execute("SELECT started_at FROM seasons WHERE guild_id=?",(i.guild_id,)).fetchone()
    start=datetime.fromisoformat(r["started_at"]); end=start+timedelta(days=SEASON_DAYS)
    await i.response.send_message(f"Temporada **{sn}**\nInício: <t:{int(start.timestamp())}:F>\nFim previsto: <t:{int(end.timestamp())}:F>")

@bot.tree.error
async def command_error(i,error):
    msg=str(error)
    if i.response.is_done(): await i.followup.send(f"Erro: {msg}",ephemeral=True)
    else: await i.response.send_message(f"Erro: {msg}",ephemeral=True)

if __name__=="__main__":
    if not TOKEN: raise RuntimeError("Configure DISCORD_TOKEN nas variáveis do Railway.")
    init_db()
    bot.run(TOKEN)
