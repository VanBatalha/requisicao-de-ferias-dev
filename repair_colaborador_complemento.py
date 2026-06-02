#!/usr/bin/env python3
"""
Repara vínculos entre colaboradores e colaborador_complemento usando o XLSX exportado.

Use este script quando o app estiver mostrando ADMIN/DP/GESTOR como USUARIO mesmo
com user_type correto no Excel. Ele recria a tabela colaborador_complemento mapeando
cada registro pelo e-mail do colaborador, não pelo ID antigo do export.

Uso:
    python repair_colaborador_complemento.py <database_url> <excel_file>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(__file__))
from ferias_app.models import Base, Colaborador, ColaboradorComplemento  # noqa: E402
from import_data import (  # noqa: E402
    clean_str,
    configure_engine_schema,
    db_schema_name,
    import_colaborador_complemento,
    import_colaboradores,
)


def main() -> None:
    if len(sys.argv) < 3:
        print("Uso: python repair_colaborador_complemento.py <database_url> <excel_file>")
        sys.exit(1)

    database_url = sys.argv[1]
    excel_file = sys.argv[2]

    if not Path(excel_file).exists():
        print(f"❌ Arquivo não encontrado: {excel_file}")
        sys.exit(1)

    engine = configure_engine_schema(create_engine(database_url, echo=False))
    print(f"🗂️ Usando schema PostgreSQL: {db_schema_name()}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    try:
        print("🔧 Atualizando dados-base dos colaboradores pelo XLSX...")
        import_colaboradores(session, excel_file)

        print("🔧 Recriando vínculos da tabela colaborador_complemento...")
        deleted = session.query(ColaboradorComplemento).delete()
        session.commit()
        print(f"   Removidos {deleted} complementos antigos.")

        import_colaborador_complemento(session, excel_file)

        email = "vanderson.batalha@certare.com.br"
        colab = session.query(Colaborador).filter_by(email=email).first()
        if colab and colab.complemento:
            print("\n✅ Conferência Vanderson:")
            print(f"   colaborador.id: {colab.id}")
            print(f"   email: {colab.email}")
            print(f"   user_type: {clean_str(colab.complemento.user_type, upper=True)}")
            print(f"   gestor_direto_email: {colab.complemento.gestor_direto_email}")
            print(f"   gestor_superior_email: {colab.complemento.gestor_superior_email}")
        else:
            print("\n⚠️ Não encontrei o registro completo do Vanderson para conferência.")

        print("\n✅ Reparo concluído. Faça logout/login no app para renovar a sessão visual.")

    except Exception as e:
        session.rollback()
        print(f"\n❌ Erro durante reparo: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
