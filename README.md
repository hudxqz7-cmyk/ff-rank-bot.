# FF Rank Bot (Enhanced)

Bot de Discord para ranking individual de partidas de Free Fire, com **ranking automático em canal dedicado**, temporadas mensais e painel de administração.

## Novidades desta versão

- **Canal exclusivo `🏆・ranking`**: o bot publica o TOP 20 uma única vez e **atualiza a mensagem automaticamente** sempre que uma partida é registrada.
- **Visual inspirado no leaderboard**: TOP 3 destacado com medalhas, avatar do líder e layout limpo.
- **Sistema de níveis**: cada 100 XP = 1 nível (inalterado).
- **Perfil completo**: Lifetime + Temporada atual (vitórias, derrotas, MVPs, partidas).
- **Temporadas**: ranking mensal (YYYY-MM) + histórico permanente (lifetime).
- **Painel de admin** (`/painel_admin`): botões rápidos para atualizar ranking e consultar classificações.
- Jogadores **não precisam** usar comandos para ver a classificação — basta olhar o canal.

## Pontuação padrão

| Situação              | XP líquido |
|-----------------------|------------|
| Vencedor              | +17 XP     |
| MVP (além da vitória) | +10 XP     |
| Perdedor              | -3 XP      |
| XP mínimo             | 0          |

O MVP **deve** pertencer ao time vencedor.

## Comandos

### Para todos
| Comando | Descrição |
|---------|-----------|
| `/ranking` | Mostra TOP 20 (lifetime ou mensal) |
| `/perfil` | Estatísticas Lifetime + Temporada atual |
| `/historico` | Últimas 10 partidas |

### Para administradores (Gerenciar servidor)
| Comando | Descrição |
|---------|-----------|
| `/setup_ranking` | Configura o canal de ranking automático |
| `/atualizar_ranking` | Força atualização da mensagem no canal |
| `/registrar_partida` | Registra partida (atualiza ranking automaticamente) |
| `/ajustar_xp` | Ajuste manual de XP (lifetime / temporada / ambos) |
| `/painel_admin` | Abre painel com botões de ação rápida |

### Apenas Administrador
| Comando | Descrição |
|---------|-----------|
| `/resetar_ranking_confirmar` | Zera dados (exige digitar `RESETAR`) |

## Como configurar o ranking automático

1. Crie um canal chamado **`🏆・ranking`** (ou qualquer canal com “ranking” no nome).
2. Dê ao bot permissão de **Ver canal**, **Enviar mensagens**, **Gerenciar mensagens** e **Incorporar links**.
3. Execute `/setup_ranking` (opcionalmente escolha o canal e o tipo: lifetime ou mensal).
4. Pronto! A mensagem será criada e atualizada sozinha a cada partida registrada.

Administradores também podem forçar a atualização com `/atualizar_ranking` ou pelo painel.

## Como criar o bot no Discord

1. Entre em https://discord.com/developers/applications.
2. **New Application** → dê um nome.
3. Aba **Bot** → **Add Bot** → copie o token.
4. Em **Privileged Gateway Intents**, ative **Server Members Intent** (necessário para nomes/avatars).
5. Em **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `View Channels`, `Manage Messages`
6. Abra o link gerado e adicione o bot ao servidor.

## Como executar

Requer Python 3.10+.

```bash
pip install -r requirements.txt
cp .env.example .env   # edite e cole o token
python bot.py
```

O arquivo `ranking.db` é criado automaticamente. **Faça backup dele** — contém todo o histórico.

## Estrutura do banco

- `players` → estatísticas lifetime
- `season_players` → estatísticas por temporada (YYYY-MM)
- `matches` → histórico de partidas
- `guild_config` → canal e mensagem de ranking por servidor

## Observações

- Dados são separados por servidor.
- O bot **não** detecta resultados do Free Fire automaticamente; um admin registra via `/registrar_partida`.
- Nunca publique o token em canais, prints ou repositórios públicos.
