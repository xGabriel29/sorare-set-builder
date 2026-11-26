# Sorare Set Builder (futebol)

Site para escolher equipas do modo **Sorare Set** (Hot Streaks): projeção de cada jogador com o modelo da escada decisiva + All-Around, odds dos jogos, % de titular, e um auto-builder que maximiza a probabilidade de cada equipa bater a meta do seu nível.

## Como funciona

- `scripts/fetch_data.py` vai buscar os jogos do Set atual, os jogadores, as galerias e as odds, e escreve `data/data.json`.
- O GitHub Actions (`.github/workflows/update.yml`) corre o script 3 vezes por dia (odds só 1 vez, para caber nos 500 créditos/mês do plano grátis) e publica o site no GitHub Pages.
- `index.html` é o site: carrega o `data/data.json` e faz toda a simulação no browser.

## Configuração

- **Segredos** (Settings → Secrets and variables → Actions): `SORARE_API_KEY` e `ODDS_API_KEY`.
- **Pages** (Settings → Pages): Source = **GitHub Actions**.
- **Galerias e bónus**: `config.json` — junta os slugs do Sorare dos teus amigos em `users` e ajusta os bónus de cada versão de carta em `editions`.

Para atualizar à mão: Actions → *Atualizar dados e publicar* → *Run workflow*.
