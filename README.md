# Azure TV

Projeto de organizacao de playlists IPTV para uso pessoal.

## Preservacao de favoritos: playlist principal protegida

A playlist principal fica no bucket privado R2, no arquivo `playlist.m3u8`. Os aplicativos que usam o Azure TV devem continuar com o endereco atual. Nao remova, reorganize ou regenere este arquivo sem testar antes: favoritos do player podem depender da identidade e da ordem dos registros.

**A antiga rotina de atualizacao completa esta PAUSADA:** [.github/workflows/azure-tv-auto.yml](.github/workflows/azure-tv-auto.yml).

## Checagem segura de URL dos canais

O workflow [.github/workflows/azure-tv-preview.yml](.github/workflows/azure-tv-preview.yml) verifica aproximadamente a cada hora se os arquivos `CanaisBRNN.m3u8` do repositorio [Ramys/Iptv-Brasil-2026](https://github.com/Ramys/Iptv-Brasil-2026) tiveram mudancas. Se os SHAs dos arquivos nao mudaram, ele nao baixa nem reprocessa a playlist.

Se houver mudancas, o script [scripts/azure_tv_preview.py](scripts/azure_tv_preview.py) compara os titulos exatos de canais ao vivo com as fontes. Ele so prepara alteracoes para URLs com correspondencia nao ambigua. Nomes, grupos, ordem, linhas de metadados e todos os URLs de filmes/series/animes permanecem identicos.

Um hash estrutural antes/depois garante que nada alem dos URLs identificados como TV seja modificado. Se nao houver URLs novos, a rotina registra a verificacao e nao publica um arquivo novo.

**NUNCA substitui a playlist principal automaticamente.** Havendo novidades, a rotina cria uma playlist separada no R2 para teste, via [scripts/azure_tv_preview_worker.js](scripts/azure_tv_preview_worker.js). O arquivo de preview so pode ser acessado usando uma chave permanente Azure TV valida. Teste as novidades antes de promover manualmente, pois alguns players podem vincular favoritos ao URL do conteudo.

Os links dos canais sao de servidores externos. Mesmo um endereco atualizado nao garante disponibilidade, autorizacao ou compatibilidade com todos os players.

Nota: o GitHub pode pausar agendas por inatividade prolongada em repositorios publicos. Acompanhe a aba Actions.

## Saude dos canais ao vivo (versao segura)

Desde outubro de 2026, o workflow [`azure-tv-preview.yml`](.github/workflows/azure-tv-preview.yml) executa os testes de regressao de `test_azure_tv_reliable.py` e a verificacao `scripts/azure_tv_reliable.py`.

A rotina verifica fingerprints dos arquivos CanaisBR da origem e do catalogo publico `iptv-org/iptv` (Brasil) a cada hora. Quando o material e os testes de saude recentes nao mudaram, ela pula a varredura pesada. Caso contrario, analisa somente canais que tenham correspondencia nominal clara e URLs alternativos.

Para uma alternativa ser considerada, ela precisa retornar um manifesto HLS valido e permitir ler um segmento de midia. Um segundo teste confirma a resposta. Se a URL atual estiver funcionando no teste, nao ha troca. Falha por bloqueio de regiao ou de um runner do GitHub nao prova que o stream esta indisponivel na casa do usuario.

**Importante:** a playlist principal `playlist.m3u8` no R2 nao recebe atualizacoes desse fluxo. O script so publica um arquivo separado de PREVIEW quando encontra alternativa validada, mantendo os mesmos registros, nomes, categorias, ordem e URLs de filmes/series. Assim, favoritos existentes nao sao afetados pela verificacao automatica.

O sistema nao garante funcionamento permanente; canais de terceiros podem sair do ar, bloquear navegadores/dispositivos ou exigir permissao de acesso. Teste alternativas no IPTV Expert antes de optar por substituir canais na principal.

Confira relatorios e resultados na aba GitHub Actions.
