# ferias_app/__init__.py

from flask import Flask
import logging

def create_app():
    app = Flask(__name__)
    
    # Configuração básica
    from .config import Config
    app.config.from_object(Config)
    
    # Inicializar extensões
    from .extensions import db, login_manager
    db.init_app(app)
    login_manager.init_app(app)
    
    # Registrar blueprints
    from .blueprints import auth, ferias, painel_dp, painel_admin
    app.register_blueprint(auth.bp)
    app.register_blueprint(ferias.bp, url_prefix='/ferias')
    app.register_blueprint(painel_dp.bp, url_prefix='/painel_dp')
    app.register_blueprint(painel_admin.bp, url_prefix='/painel_admin')
    
    # Criar tabelas (apenas em desenvolvimento)
    with app.app_context():
        try:
            db.create_all()
            app.logger.info("✅ Tabelas criadas/verificadas com sucesso")
        except Exception as e:
            app.logger.error(f"❌ Erro ao criar tabelas: {e}")
    
    # Inicializar scheduler com tratamento de erro
    try:
        from .services.scheduler_service import start_scheduler
        with app.app_context():
            start_scheduler()
        app.logger.info("✅ Scheduler iniciado com sucesso")
    except Exception as e:
        app.logger.warning(f"⚠️ Scheduler falhou ao iniciar (app continuará sem sync automático): {e}")
        # Não falhar o app se o scheduler falhar
    
    # Rota de teste para verificar se o app está rodando
    @app.route('/health')
    def health_check():
        return {'status': 'ok', 'message': 'App está rodando'}, 200
    
    return app

# Criar a instância do app
app = create_app()

if __name__ == '__main__':
    app.run(debug=True)
