# Auditoria — Pipoca & Play

## Diagnóstico inicial

O repositório original continha uma interface de questionário e uma função de recomendação OpenAI, mas não possuía landing page, tela de login, sessão, papéis de usuário, painel administrativo nem proteção da rota `/api/recommend`. O histórico dependia exclusivamente de `window.storage`, que não existe em navegadores comuns. A interface já tinha os sete filtros do MVP e uma experiência de resultados com três cartões.

## Correções aplicadas

A aplicação agora possui landing page pública, tela de login e cadastro individual, sessão por cookie HttpOnly assinado com HMAC, papel `admin`, painel `/api/admin/status`, logout e proteção de recomendações no backend. Cada conta de cliente possui hash PBKDF2, status `pending`, `approved` ou `rejected`, e o frontend acompanha a situação do pedido em `/api/auth/status`.

O painel administrativo passou a listar pedidos e contas, com ações protegidas para aceitar, rejeitar ou excluir usuários. A base usa JSON local em desenvolvimento e Redis REST compatível com Vercel KV/Upstash em produção serverless; sem armazenamento persistente configurado em produção, o cadastro é bloqueado com resposta controlada.

O backend agora valida o JSON da IA e exige exatamente três recomendações ordenadas. Foi criado `ContentMetadataProvider`, com adaptador TMDB opcional para poster, backdrop, duração, gêneros e disponibilidade brasileira. Sem fonte externa configurada, a interface exibe disponibilidade não confirmada em vez de atribuir fatos ao modelo.

O histórico ganhou fallback para `localStorage`, mantendo compatibilidade com o armazenamento do ambiente quando disponível. A documentação e `.env.example` foram atualizados com as variáveis de autenticação, catálogo e persistência.

## Validação visual local

A landing page foi aberta em `http://127.0.0.1:8000/` e exibiu corretamente CTA, proposta de valor e os três indicadores. O botão de entrada abriu a tela de login com abas de entrada e cadastro; a aba de cadastro exibiu e-mail, senha e confirmação de senha.

Foi criado um cadastro de teste com e-mail e senha individuais. A interface exibiu a mensagem de aguardo — “O seu acesso será liberado assim que o administrador validar. Por favor, aguarde a liberação.” — e o status ficou disponível para atualização automática.

O login local administrativo de teste `admin@test.local` / `admin-password` foi aceito e criou sessão. O painel exibiu uma pendência real com as ações “Aceitar acesso”, “Rejeitar” e “Excluir usuário”. Após aceitar o cadastro, os contadores mudaram para 0 pendências e 1 acesso liberado, e a conta do cliente entrou na lista como aprovada. O login do cliente com a senha cadastrada abriu corretamente o primeiro filtro da plataforma.

A partir do painel, o botão “Nova busca” continua abrindo o primeiro filtro e a seleção “Suspense / Thriller” avança automaticamente para o segundo filtro, mantendo os sete passos oficiais.

O fluxo autenticado também avançou corretamente pela vibe “Sombrio / Assustador” e pelo tempo “Padrão (90 a 120 min)”, confirmando a progressão visual e a separação entre clima e duração.

O wizard avançou corretamente por “Anos 2010s” e “Netflix”, confirmando também o filtro de plataforma independente dos demais.

As últimas etapas “Em Casal” e “Aclamados pela Crítica / Premiações (Oscar, Cannes)” levaram ao resumo “Sua busca”, que exibiu corretamente todos os sete valores antes da execução.

Após reiniciar com um `.env` de teste, uma sessão antiga foi invalidada e o navegador voltou à landing page. A tela de login aceitou o formulário de `cliente@pipocaplay.com`; o teste HTTP correspondente confirmou papel `user` e bloqueio de `/api/admin/status` com HTTP 403.

No navegador, o usuário comum foi redirecionado ao primeiro filtro sem botão de painel admin. O botão “Histórico” abriu o estado vazio corretamente, confirmando que a ausência de `window.storage` não quebra a página graças ao fallback para `localStorage`.

Foi simulada uma resposta JSON válida no navegador para não depender de chave OpenAI durante o teste. `runSearch` terminou em `results`, renderizou exatamente três recomendações, match, ratings, Oscar, links de verificação, fallback visual e aviso de disponibilidade não confirmada; também salvou um item no histórico via `localStorage`.

## Correções de persistência e acesso administrativo

O cadastro em produção falhava com “Não foi possível gravar a base de contas. Configure um armazenamento persistente.” porque o deploy não tinha nenhum armazenamento conectado e o código caía no arquivo local, que o filesystem somente leitura das funções serverless recusa.

A camada de armazenamento passou a detectar quatro backends, nesta ordem: `postgres` (`POSTGRES_URL`/`DATABASE_URL`), `vercel-blob` (`BLOB_READ_WRITE_TOKEN`), `redis-rest` (`KV_REST_API_URL`) e `arquivo-local`. O Postgres é a opção recomendada, cria a tabela `pipoca_play_store` sozinha na primeira gravação e usa `psycopg[binary]`, já declarado em `requirements.txt`. A gravação local agora recusa explicitamente o modo serverless e a mensagem de erro passou a nomear o passo a passo real na Vercel, em vez de pedir genericamente “um armazenamento persistente”. Foi adicionado `GET /api/health`, público e sem segredos, que informa qual backend está ativo, se ele é persistente e se as credenciais de administrador existem.

O acesso administrativo ganhou página dedicada em `/admin`, com login próprio, contadores, busca, filtro por situação e as ações de liberar, rejeitar, voltar para pendente, redefinir senha, promover, rebaixar, excluir e criar acesso já liberado. Além do administrador raiz definido por `ADMIN_EMAIL`/`ADMIN_PASSWORD`, contas gravadas na base podem receber o papel `admin` e entrar pelo mesmo painel. O administrador logado é impedido, no backend e na interface, de retirar o próprio acesso. O painel exibe um bloco de diagnóstico com o backend em uso e as variáveis encontradas, e mostra o passo a passo da Vercel quando nenhum banco está conectado.

As rotas de autenticação e administração passaram a compartilhar `api_core.py`, chamado tanto por `server.py` quanto pelas funções em `api/`, eliminando a duplicação que fazia o servidor local e o runtime serverless divergirem. A suíte `test_app.py` subiu de 11 para 27 testes, cobrindo o backend Postgres com driver falso, a recusa de gravação em modo serverless, o ciclo completo de ações administrativas, a proteção da própria conta, o payload de `/api/health` e as respostas de login.

## Rodada 2 — assinatura, créditos, marcações e checkout

### Diagnóstico

O deploy publicado (commit `919e061`) tinha landing page, cadastro com aprovação, painel `/admin` e o motor de sete filtros, mas **nenhuma** das funcionalidades de monetização e personalização pedidas: não havia catálogo de planos, contagem de créditos, marcações do usuário, checkout, integração com a Asaas, controle do botão voltar, encerramento de sessão ao sair da página nem exibição da senha digitada. O código também não continha qualquer referência à Asaas — a integração precisou ser escrita do zero.

### Correções aplicadas

Foram criados `plans.py` (catálogo Silver/Gold/Diamante), `billing.py` (cliente da API Asaas, validação de CPF/CNPJ, assinatura mensal, URL de fatura e leitura do webhook) e `recommender.py` (motor extraído de `server.py`, agora compartilhado sem divergência entre o servidor local e as funções serverless). O `user_store.py` ganhou assinatura, ciclo de 30 dias, créditos diários no fuso de Brasília e as marcações por título; o `api_core.py` ganhou as rotas de conta, planos, marcações, checkout, conferência de pagamento, webhook e a rota de recomendação com débito de crédito e devolução em caso de falha do motor.

Na interface, o cliente sem assinatura cai na vitrine de planos, paga pela Asaas e só recebe indicações após a confirmação; cada card traz *gostei*, *não gostei* e *já assisti*, gravados na conta e injetados no prompt; a tela **Marcações** permite remover item por item; a navegação usa a History API para o botão voltar nativo; a sessão virou cookie de sessão do navegador com logout no `pagehide`; e os campos de senha ganharam o ícone de olho.

### Validação

`python3 -m unittest test_app` — 51 testes, todos verdes, incluindo consumo e reposição de créditos por plano, bloqueio sem pagamento, webhook com e sem token válido, suspensão por inadimplência e remoção individual de marcações.

Fluxo HTTP completo executado contra o servidor real com Asaas e OpenAI simulados: cadastro → aprovação pelo admin → login → bloqueio sem plano → checkout → webhook de confirmação → duas consultas do plano Silver → bloqueio por falta de crédito → marcação enviada ao prompt → remoção da marcação → sessão encerrada ao sair.

Interface validada em Chromium (390×844, sem erros de JavaScript no console): olho da senha, vitrine com os três preços, checkout com recusa de CPF inválido, espera pela confirmação, liberação após o webhook, contador de créditos no topo, marcação nos cards, histórico de marcações, aviso de limite diário, remoção individual e retorno pelo botão voltar. Recarregar a página desconecta o cliente e exige novo login, como especificado.
