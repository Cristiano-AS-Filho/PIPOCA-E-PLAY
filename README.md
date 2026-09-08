# Pipoca & Play

**Pipoca & Play** é uma plataforma SaaS de recomendação personalizada de filmes. O cliente cria seu acesso com e-mail e senha, aguarda a validação do administrador e, depois da liberação, responde aos sete filtros do MVP para receber exatamente três opções ordenadas por compatibilidade, com explicação curta e histórico local da sessão.

## Funcionalidades disponíveis

A experiência pública começa em uma **landing page** responsiva, com apresentação do produto e CTA de entrada. A autenticação usa sessão por cookie `HttpOnly`, `SameSite=Lax`, assinatura HMAC e expiração automática. O cadastro exige senha individual com hash PBKDF2, começa com status `pending` e pode ser acompanhado automaticamente pelo cliente. O papel `admin` abre um painel restrito para listar pedidos, aceitar, rejeitar ou excluir usuários.

A lógica oficial dos sete filtros foi preservada: gênero principal, humor/vibe do dia, tempo disponível, época do filme, plataforma de streaming, companhia e popularidade/estilo. O backend valida o JSON da IA e exige três recomendações ordenadas. A pontuação exibida é um **match próprio do sistema**, não uma nota de IMDb ou crítica.

O enriquecimento factual é separado da IA. Quando `TMDB_API_KEY` está configurada, o adaptador consulta posters, backdrops, duração, gêneros e disponibilidade no Brasil. Sem essa chave, o sistema informa explicitamente que a disponibilidade não foi confirmada e oferece links de conferência no JustWatch, IMDb e Letterboxd; o modelo nunca é tratado como banco de dados.

## Painel do administrador (`/admin`)

O painel administrativo tem **página e login próprios** em `https://SEU-DOMINIO/admin`. Ele não depende da navegação da plataforma: abra o endereço, entre com as credenciais de administrador e você já cai na lista de acessos.

Existem dois caminhos para ter uma conta de administrador:

1. **Administrador raiz por variável de ambiente.** Defina `ADMIN_EMAIL` e `ADMIN_PASSWORD` no projeto. Esse acesso funciona mesmo com a base de contas vazia e é o que destrava o painel na primeira vez. Sem essas duas variáveis em produção, **nenhum** login de administrador é aceito.
2. **Administradores gravados na base.** Já dentro do painel, use “Tornar admin” em qualquer conta liberada, ou crie uma conta com o papel *Administrador* no bloco “Criar acesso manualmente”. Esses administradores entram pelo mesmo `/admin` com o e-mail e a senha deles.

No painel você vê os contadores (pendentes, liberados, rejeitados, administradores, total), busca por e-mail, filtra por situação e, em cada conta, pode **liberar**, **rejeitar**, **voltar para pendente**, **definir uma nova senha**, **promover/rebaixar** e **excluir**. Há ainda um bloco para criar um acesso já liberado, sem passar pela fila de aprovação. Por segurança, o administrador logado não consegue rejeitar, excluir, despromover nem voltar a própria conta para pendente — outra conta de administrador precisa fazer isso.

O último bloco, **Diagnóstico do deploy**, mostra em qual armazenamento as contas estão sendo gravadas e quais variáveis o ambiente encontrou. A mesma informação, sem nenhum segredo, está disponível publicamente em `GET /api/health` — é o endereço mais rápido para descobrir por que um cadastro falhou.

## Banco de contas

O backend de contas é escolhido automaticamente a partir das variáveis presentes no ambiente, nesta ordem:

| Prioridade | Modo | Variáveis | Onde criar |
| --- | --- | --- | --- |
| 1 | `postgres` | `POSTGRES_URL` ou `DATABASE_URL` | Vercel → Storage → Create Database → Neon Postgres |
| 2 | `vercel-blob` | `BLOB_READ_WRITE_TOKEN` | Vercel → Storage → Create → Blob (acesso privado) |
| 3 | `redis-rest` | `KV_REST_API_URL` + `KV_REST_API_TOKEN` | Vercel → Storage → Upstash for Redis |
| 4 | `arquivo-local` | `USER_STORE_FILE` | apenas desenvolvimento local |

**O filesystem das funções serverless é temporário.** Se o deploy de produção subir sem nenhuma das três primeiras opções, o cadastro responde `503` com a mensagem que explica exatamente o que fazer, em vez de fingir que gravou. Para resolver:

1. Abra o projeto na Vercel → aba **Storage** → **Create Database**.
2. Escolha **Neon Postgres** (recomendado), **Upstash for Redis** ou **Blob** e conecte ao projeto.
3. A Vercel injeta a variável correspondente automaticamente em todos os ambientes.
4. Faça um novo deploy (**Deployments → Redeploy**) para as variáveis entrarem em vigor.

No modo `postgres`, a tabela `pipoca_play_store` é criada sozinha na primeira gravação; não é preciso rodar nenhuma migração. O driver `psycopg[binary]` já está em `requirements.txt`.

### Usando Supabase em vez de Neon

O modo `postgres` funciona com qualquer Postgres, incluindo Supabase — mas **use a string de "Transaction pooler"**, nunca a de conexão direta. No painel do Supabase: **Project Settings → Database → Connection string → aba "Transaction pooler"**. Ela tem o formato:

```
postgresql://postgres.<ref-do-projeto>:[SUA-SENHA]@aws-0-<região>.pooler.supabase.com:6543/postgres
```

A conexão direta (`db.<ref-do-projeto>.supabase.co:5432`, mostrada na aba "URI") só tem endereço **IPv6**, e as funções serverless da Vercel não têm saída IPv6 — a conexão falha mesmo com a senha certa. Se isso acontecer, `/api/health` e a mensagem de erro do cadastro já apontam essa causa específica.

### Erro "invalid URI query parameter"

Se `/api/health` mostrar algo como `ProgrammingError: invalid URI query parameter: "supa"`, a string de conexão salva em `POSTGRES_URL`/`DATABASE_URL` na Vercel está corrompida — normalmente sobrou um pedaço de texto depois de um `&` (por exemplo, um fragmento de "supabase" colado pela metade). O `/api/health` já aponta qual variável e qual parâmetro é o culpado. Para corrigir:

1. Vercel → **Settings → Environment Variables**, abra a variável indicada no erro.
2. Apague o valor inteiro e copie a Connection string novamente, sem editar manualmente (Supabase: aba "Transaction pooler"; Neon: variável injetada automaticamente).
3. Salve e faça um novo deploy (**Deployments → Redeploy**).

## Executar localmente

1. Copie `.env.example` para `.env`.
2. Mantenha `ENVIRONMENT` diferente de `production` para executar localmente. O admin de desenvolvimento usa `admin@pipocaplay.com` / `admin123`; troque esses valores antes de qualquer uso real.
3. Defina `AUTH_SECRET`, `ADMIN_EMAIL` e `ADMIN_PASSWORD` com valores privados e fortes. Não publique `.env`.
4. Em desenvolvimento, `USER_STORE_FILE=data/users.json` cria a base local automaticamente.
5. Defina `OPENAI_API_KEY` e, se disponível, `TMDB_API_KEY`.
6. Execute `python3 server.py` dentro desta pasta.
7. Abra `http://127.0.0.1:8000` para a plataforma e `http://127.0.0.1:8000/admin` para o painel.

A chave da OpenAI é lida apenas pelo servidor. O navegador envia os sete filtros para `POST /api/recommend` apenas depois do login.

Os testes rodam com `python3 -m unittest test_app`.

## Rotas

| Rota | Método | Acesso | Uso |
| --- | --- | --- | --- |
| `/` | GET | público | Landing page e plataforma |
| `/admin` | GET | público (a página), painel só com sessão admin | Painel do administrador |
| `/api/health` | GET | público | Diagnóstico do deploy, sem segredos |
| `/api/auth/register` | POST | público | Cadastro do cliente (entra como `pending`) |
| `/api/auth/login` | POST | público | Cria a sessão por cookie |
| `/api/auth/logout` | POST | público | Encerra a sessão |
| `/api/auth/me` | GET | público | Sessão atual |
| `/api/auth/status` | GET | token do pedido | Acompanhamento do cadastro |
| `/api/admin/status` | GET | admin | Configuração, contadores e lista de contas |
| `/api/admin/users` | GET/POST | admin | Ações administrativas |
| `/api/recommend` | POST | usuário autenticado | Motor de recomendação |

As ações aceitas em `POST /api/admin/users` são `approve`, `reject`, `pending`, `delete`, `promote`, `demote`, `set_password` e `create`.

## Publicação na Vercel

O arquivo `vercel.json` e as funções em `api/` deixam o repositório pronto para a Vercel. Em **Project Settings → Environment Variables**, configure, por ambiente:

| Variável | Obrigatória | Uso |
| --- | --- | --- |
| `OPENAI_API_KEY` | Sim | Chave privada para gerar recomendações. |
| `OPENAI_MODEL` | Sim | Modelo disponível no projeto OpenAI. |
| `AUTH_SECRET` | Sim | Segredo longo e aleatório para assinar sessões. |
| `ADMIN_EMAIL` | Sim | E-mail do administrador que abre o painel `/admin`. |
| `ADMIN_PASSWORD` | Sim | Senha privada do administrador. |
| `POSTGRES_URL` / `DATABASE_URL` | Uma das três | Banco Postgres das contas; injetada ao conectar o Neon. |
| `BLOB_READ_WRITE_TOKEN` | Uma das três | Token da loja Vercel Blob privada. |
| `KV_REST_API_URL` + `KV_REST_API_TOKEN` | Uma das três | Redis REST (Upstash / Vercel KV). |
| `USER_STORE_BLOB_PATH` | Não | Objeto privado do Blob; padrão `pipoca-play/users.json`. |
| `USER_STORE_FILE` | Local | Caminho do JSON local; não persiste em produção serverless. |
| `USER_STORE_MODE` | Não | Força um backend específico; use apenas para depurar. |
| `TMDB_API_KEY` | Não | Habilita enriquecimento de catálogo e disponibilidade no Brasil. |
| `ENVIRONMENT=production` | Recomendada | Desativa credenciais padrão de desenvolvimento e ativa cookies seguros. |

O cadastro de clientes segue os estados `pending`, `approved` e `rejected`. Somente contas `approved` conseguem criar sessão e usar o motor de recomendação, e toda rota administrativa exige uma sessão com papel `admin`. Depois do deploy, confira `GET /api/health`: se `storage.persistent` vier `false`, o banco ainda não está conectado. Recuperação de senha pelo próprio cliente, e-mail transacional e auditoria de ações administrativas permanecem como evoluções futuras.

Não coloque chaves no HTML, no Git ou em mensagens de erro. Se uma chave tiver sido exposta anteriormente, revogue-a no respectivo provedor antes de publicar.
