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

## Assinatura, créditos, marcações e navegação

O pedido pressupunha que a integração com o ASAAS já estivesse no repositório. Ela não estava: uma busca por `asaas`, `payment`, `checkout`, `plano` e `credit` em todo o código não retornou nenhuma ocorrência, e não havia cobrança, plano nem controle de consumo em lugar algum. Toda a camada de pagamento descrita abaixo foi escrita nesta rodada.

Foi criado `billing.py`, que reúne o catálogo dos três planos (Silver R$ 15,00 com 2 créditos diários, Gold R$ 25,00 com 5, Diamante R$ 30,00 ilimitado no ciclo de 30 dias), o cliente HTTP do ASAAS e as regras de crédito. `user_store.py` ganhou os campos `subscription`, `credits` e `marks` por conta, com consumo e estorno de crédito sob o mesmo lock que já protegia a base, de modo que duas buscas simultâneas não gastam o mesmo crédito. O dia do crédito vira à meia-noite de Brasília (UTC−03), não à meia-noite UTC.

O motor de recomendação passou a ser uma rota paga. `api_core.recommend` exige sessão, assinatura com pagamento confirmado e crédito disponível, e responde `402` com `reason` igual a `payment_required` ou `no_credits`; o front usa esse motivo para levar o cliente à tela de planos em vez de mostrar um erro genérico. `server.py` e `api/recommend.py` passaram a chamar essa mesma função, eliminando a duplicação que existia entre o servidor local e o runtime serverless. O crédito é debitado antes da chamada ao modelo e estornado quando ela falha, comportamento coberto por teste.

A confirmação do pagamento tem dois caminhos independentes. O webhook `POST /api/billing/webhook` valida o cabeçalho `asaas-access-token` contra `ASAAS_WEBHOOK_TOKEN` com comparação de tempo constante e recusa todos os eventos quando a variável não está configurada — um webhook aberto deixaria qualquer um liberar acesso pago. Em paralelo, `GET /api/billing/status` consulta as cobranças da assinatura no ASAAS a cada leitura, para que o acesso seja liberado mesmo se o webhook não chegar. O ciclo de 30 dias é reescrito apenas quando a confirmação é mais nova que a já gravada, então consultas repetidas não estendem o acesso indevidamente.

As marcações “gostei”, “não gostei” e “já assisti” passaram a ser gravadas na conta do usuário, com chave derivada do título e do ano para evitar duplicatas quando a mesma obra volta a aparecer. Opinião e “já assisti” são independentes, e limpar as duas apaga a marcação. `buildRecommendationPrompt` passou a receber essas preferências e a montar um bloco que proíbe recomendar o que já foi assistido e orienta o modelo a favor ou contra títulos parecidos; sem marcações o bloco desaparece por completo e o prompt volta a ser exatamente o do MVP. A tela “Marcações” lista o histórico com remoção individual, que devolve o título às indicações futuras.

A navegação passou a usar `history.pushState` por tela, com um manipulador de `popstate` que restaura a tela anterior. O botão voltar nativo do celular percorre os sete filtros, o resumo, os resultados, os planos, o checkout e as marcações sem recarregar a página. O cookie de sessão perdeu `Max-Age` e `Expires`, tornando-se um cookie de sessão de navegador, e o front dispara `navigator.sendBeacon` para `/api/auth/logout` no evento `pagehide`, de modo que sair da página exige um novo login. A fatura do ASAAS abre em nova aba justamente para não disparar esse encerramento, e a ida ao painel `/admin` marca uma saída intencional.

O painel administrativo ganhou a coluna “Assinatura”, com o plano e a situação do pagamento de cada conta, e o contador de assinaturas ativas.

## Validação desta rodada

A suíte subiu de 30 para 53 testes, cobrindo os preços e créditos dos três planos, o bloqueio antes da confirmação do pagamento, o esgotamento e a virada diária dos créditos, o estorno em caso de falha do modelo, a expiração do ciclo, a recusa do webhook sem token, a independência entre opinião e “já assisti”, o isolamento das marcações entre contas, a presença das marcações no prompt e o cookie sem expiração.

O caminho HTTP do ASAAS foi exercitado contra um stub da API que valida o cabeçalho `access_token`: criação de cliente, criação de assinatura, cobrança pendente que mantém o acesso fechado e cobrança confirmada que o abre, com os cinco créditos diários do plano Gold se esgotando na sexta consulta.

A interface foi verificada em navegador (Chromium, viewport de 412 px). Um cliente sem plano cai na tela de planos com os três preços e os créditos anunciados; o checkout abre e o botão voltar nativo retorna aos planos. Um cliente com plano pago abre direto no primeiro filtro, com o chip “5/5 HOJE” no topo; o botão voltar percorre os filtros para trás e o avançar refaz o caminho. A tela de marcações lista a marcação existente e a remoção individual esvazia a lista. Nos cards, os três botões marcam e desmarcam corretamente, “gostei” e “já assisti” convivem, “não gostei” substitui “gostei”, e o estado gravado no servidor confere. Ao sair da página e voltar, a plataforma abre deslogada na landing.

## Correção do deploy na Vercel

O redeploy passou a falhar com “The deployment failed because of a project or build error”. Os catorze módulos em `api/` importam sem erro quando carregados isoladamente, então não era falha de código: as cinco rotas novas levaram o projeto de nove para catorze funções, acima do teto de doze funções por deploy do plano Hobby.

As rotas irmãs foram agrupadas: `api/auth.py` responde por login, logout, me, register e status; `api/admin.py` por status e users; `api/billing.py` por plans, status, checkout e webhook. Os arquivos individuais foram removidos e o `vercel.json` ganhou `rewrites` que levam `/api/<grupo>/<ação>` até a função do grupo com a ação em `?__route=`. As URLs públicas continuam iguais, e o projeto caiu de catorze para seis funções.

O `route_action` de `serverless_utils.py` resolve a ação lendo primeiro o segmento do caminho e só depois o parâmetro do rewrite, e só devolve valores de uma lista fechada — nunca texto arbitrário da requisição. Assim a função responde certo tanto se a Vercel preservar o caminho original quanto se entregar apenas o caminho reescrito.

A suíte subiu para 71 testes. Os novos exercitam cada rota agrupada nas duas formas de caminho, a recusa de método errado e de ação inexistente, o webhook com e sem o token correto, e duas travas de regressão: a contagem de funções em `api/` não pode passar de doze, e toda rota `/api/...` chamada pelo frontend precisa ter função própria ou rewrite.

