# Azure TV

Projeto de organizacao de playlists IPTV para uso pessoal.

## Sincronizacao automatica

A rotina em [.github/workflows/azure-tv-auto.yml](.github/workflows/azure-tv-auto.yml) verifica, aproximadamente uma vez por hora, as listas do repositorio [Ramys/Iptv-Brasil-2026](https://github.com/Ramys/Iptv-Brasil-2026), no branch master.

Ela acompanha os arquivos CanaisBRNN.m3u8 e Filmes-Series.m3u8. Alteracoes apenas no README ou em outros arquivos nao exigem republicacao.

Quando um arquivo monitorado muda, o script [scripts/sync_azure_tv.py](scripts/sync_azure_tv.py) baixa as fontes de uma revisao fixa, gera a playlist, aplica filtros e valida antes de enviar.

Filtros: excluir conteudo adulto por nomes/categorias, URLs repetidas e canais de TV nao identificados como HD ou FHD. Esses filtros sao heuristicos e podem precisar de ajustes se as fontes mudarem.

O upload usa autenticacao GitHub Actions OIDC, sem guardar credenciais do R2 no repositorio. O Worker independente de upload esta em [scripts/azure_tv_sync_worker.js](scripts/azure_tv_sync_worker.js); aceita apenas tokens validos deste repositorio e workflow no branch main.

O arquivo final substitui playlist.m3u8 no bucket privado sem alterar o link permanente usado no player. O gateway preserva o primeiro backup historico e as duas versoes anteriores mais recentes.

Em caso de falha de download, validacao ou upload, a rotina retorna erro e evita substituir uma lista incompleta. Uma playlist valida nao garante que todos os canais ou servidores IPTV estejam online.

Nota: O GitHub pode desativar workflows agendados em repositorios publicos apos longos periodos de inatividade. Verifique a aba Actions e reative caso necessario.
