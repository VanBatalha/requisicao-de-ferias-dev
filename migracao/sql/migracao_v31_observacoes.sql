-- V31 nao exige nova alteracao estrutural obrigatoria.
-- Esta versao corrige a regra de calculo:
-- - AJUSTE negativo reduz saldo_periodo;
-- - saldo_disponivel pode ficar negativo;
-- - tela passa a exibir saldo vindo de saldo_periodo sem travar em zero;
-- - autocomplete passa a usar dropdown proprio, limitado visualmente a 5 itens com rolagem.

-- Caso a coluna ainda nao exista por algum motivo, garanta-a:
ALTER TABLE app_ferias.solicitacoes_ferias
ADD COLUMN IF NOT EXISTS periodo_aquisitivo_origem TEXT;
