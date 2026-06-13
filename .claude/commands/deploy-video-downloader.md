---
description: Revisa README/.gitignore/roadmap, commita e dá push no main
---

Você vai executar o ciclo de release deste projeto. Faça TUDO abaixo na ordem,
sem pular passos e sem pedir confirmação intermediária (o usuário invocou
este comando explicitamente para automatizar a sequência).

## 1. Inspeção

Em paralelo (uma única mensagem com múltiplas chamadas de Bash):

- `git status`
- `git diff --stat`
- `git log --oneline -5`
- `grep -n "^APP_VERSION" app.py`

## 2. Revisar `.gitignore`

Garanta que estes padrões existem (adicione os que faltarem, preservando os
demais):

- `.env`
- `roadmap.txt`   ← obrigatório; `roadmap.txt` NUNCA é commitado
- `__pycache__/`
- `*.py[cod]`
- `.venv/` / `venv/`
- `.vscode/` / `.idea/` / `.DS_Store`
- `*.log`

## 3. Revisar `README.md`

- Confirmar que o changelog reflete a versão atual de `APP_VERSION` (ler em
  `app.py`). Se houver mudanças no diff que ainda não estão documentadas,
  adicione uma entrada no topo do changelog descrevendo o que mudou.
- Conferir tabela de variáveis de ambiente vs. `app.py` (`os.environ.get`).
  Toda env nova precisa aparecer na tabela com default + descrição.
- Conferir que o exemplo do `curl /healthz` cita a versão correta.

## 4. Revisar `roadmap.txt`

Arquivo é local (gitignored). Marcar `[DONE vX.Y.Z]` em itens que foram
implementados desde a última edição. Não criar entradas novas a menos que
algo relevante tenha aparecido no diff. Manter a seção "RESUMO ATUAL" e
"PRÓXIMA FASE SUGERIDA" atualizadas.

## 5. Validação

Antes de commitar:

```bash
python -m py_compile app.py db.py download_videos.py
```

Se houver mudança em código Python ou Dockerfile, rebuilde e cheque o
healthz:

```bash
docker compose up -d --build
sleep 4 && curl -fsS http://localhost:5000/healthz
```

A resposta deve conter `"ok":true` e a versão atual.

## 6. Commit + push

- `git add` SOMENTE os arquivos rastreáveis (NUNCA `roadmap.txt` nem `.env`).
- Mensagem de commit em português, no estilo dos commits anteriores do
  repositório (`Docs:`, `Fix:`, `Update vX:`, etc.), descrevendo o "porquê"
  em 1-2 linhas e listando o que mudou em bullets se forem várias coisas.
- Use HEREDOC pra preservar formatação:

  ```bash
  git commit -m "$(cat <<'EOF'
  <título>

  <corpo opcional>
  EOF
  )"
  ```

- `git status` após o commit para confirmar working tree limpo (exceto
  `roadmap.txt` e quaisquer locais não commitáveis).
- `git push`.

## 7. Resumo final

Imprima ao usuário:

- Hash do commit criado.
- Versão atual (`APP_VERSION`).
- Resultado do `/healthz` (se foi executado).
- Qualquer arquivo deixado de fora propositalmente (ex: roadmap).

## Regras

- NÃO commitar `roadmap.txt`, `.env`, `__pycache__/` ou qualquer artefato
  local. Se aparecerem em `git status`, é sinal de que faltou padrão no
  `.gitignore` — adicione antes de commitar.
- NÃO usar `--no-verify` nem desabilitar hooks.
- NÃO fazer force-push.
- Se o push falhar por divergência, NÃO resolver com `--force`. Pare e
  reporte ao usuário.
- Se `py_compile` ou `/healthz` falharem, NÃO commitar. Reporte e pare.
