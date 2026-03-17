import os
import warnings
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit, disconnect, join_room
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Подавляем предупреждения для чистого лога
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning, module='sqlalchemy')

# === Инициализация приложения ===
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# === Настройка базы данных (универсальная для Railway/локально) ===
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # Исправляем формат для psycopg2
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    # Локально используем SQLite
    basedir = os.path.abspath(os.path.dirname(__file__))
    db_path = os.path.join(basedir, 'instance', 'chat.db')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

print(f"🗄️ База данных: {app.config['SQLALCHEMY_DATABASE_URI']}")

# === Настройки для загрузки файлов ===
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# Создаём папки для загрузок (локально)
if not database_url:  # Только локально, на Railway файловая система временная
    for folder in ['avatars', 'images', 'videos', 'files', 'circles', 'group_avatars']:
        os.makedirs(os.path.join(UPLOAD_FOLDER, folder), exist_ok=True)

# === Cloudinary настройка (опционально, для продакшена) ===
CLOUDINARY_ENABLED = os.environ.get('CLOUDINARY_CLOUD_NAME') is not None
if CLOUDINARY_ENABLED:
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(
            cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
            api_key=os.environ.get('CLOUDINARY_API_KEY'),
            api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
            secure=True
        )
        print("☁️ Cloudinary подключён")
    except ImportError:
        print("⚠️ Cloudinary библиотека не установлена, используем локальное хранилище")
        CLOUDINARY_ENABLED = False

# === Инициализация расширений ===
db = SQLAlchemy(app)

# WebSocket: используем eventlet для асинхронности (поддерживается Railway)
async_mode = 'eventlet' if not os.environ.get('DISABLE_EVENTLET') else None
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode, manage_session=False)

# === Глобальное хранилище подключений для статусов онлайн ===
online_users = {}  # {user_id: sid}

# === Таблица связи пользователей и групп ===
group_members = db.Table('group_members',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('chat_group.id'), primary_key=True)
)

# === Вспомогательные функции ===
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Пожалуйста, войдите в систему', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def notify_user_updated(user):
    """Оповещает всех, что профиль пользователя обновился"""
    socketio.emit('user_updated', user.to_dict(), broadcast=True)

# === Модели базы данных ===
class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    bio = db.Column(db.String(280), default='')
    avatar_filename = db.Column(db.String(256), default='default.png')
    last_seen = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    groups = db.relationship('ChatGroup', secondary=group_members, backref='members')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def avatar_url(self):
        if self.avatar_filename and self.avatar_filename != 'default.png':
            if CLOUDINARY_ENABLED:
                return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/image/upload/{self.avatar_filename}"
            return url_for('static', filename=f'uploads/avatars/{self.avatar_filename}')
        return f'https://ui-avatars.com/api/?name={self.username}&background=random&color=fff'
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'avatar': self.avatar_url(),
            'bio': self.bio,
            'last_seen': self.last_seen.strftime('%Y-%m-%d %H:%M') if self.last_seen else None
        }


class ChatGroup(db.Model):
    __tablename__ = 'chat_group'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    avatar_filename = db.Column(db.String(256), default='default_group.png')
    
    creator = db.relationship('User', foreign_keys=[creator_id], backref='created_groups')
    
    def avatar_url(self):
        if self.avatar_filename and self.avatar_filename != 'default_group.png':
            if CLOUDINARY_ENABLED:
                return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/image/upload/{self.avatar_filename}"
            return url_for('static', filename=f'uploads/group_avatars/{self.avatar_filename}')
        return f'https://ui-avatars.com/api/?name={self.name}&background=0084ff&color=fff'
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'avatar': self.avatar_url(),
            'creator_id': self.creator_id,
            'member_count': len(self.members),
            'created_at': self.created_at.strftime('%d.%m.%Y')
        }


class Message(db.Model):
    __tablename__ = 'message'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    group_id = db.Column(db.Integer, db.ForeignKey('chat_group.id'), nullable=True)
    text = db.Column(db.String(1000), nullable=True)
    media_filename = db.Column(db.String(256), nullable=True)
    media_type = db.Column(db.String(20), nullable=True)
    is_private = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    sender = db.relationship('User', foreign_keys=[sender_id], backref='sent_messages')
    recipient = db.relationship('User', foreign_keys=[recipient_id], backref='received_messages')
    group = db.relationship('ChatGroup', foreign_keys=[group_id], backref='messages')
    
    def to_dict(self):
        data = {
            'id': self.id,
            'sender': self.sender.to_dict(),
            'text': self.text,
            'timestamp': self.timestamp.strftime('%H:%M'),
            'is_private': self.is_private,
            'recipient_id': self.recipient_id,
            'group_id': self.group_id
        }
        if self.media_filename:
            # 🔥 ИСПРАВЛЕНО: Правильные имена папок
            if self.media_type == 'circle':
                folder = 'circles'
            elif self.media_type == 'image':
                folder = 'images'
            elif self.media_type == 'video':
                folder = 'videos'
            else:
                folder = 'files'
            
            if CLOUDINARY_ENABLED:
                # Для Cloudinary media_filename уже содержит полный URL
                data['media_url'] = self.media_filename
            else:
                data['media_url'] = url_for('static', filename=f'uploads/{folder}/{self.media_filename}')
            data['media_type'] = self.media_type
        return data


# === Маршруты: Аутентификация ===
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Заполните все поля', 'error')
            return redirect(url_for('register'))
        if User.query.filter_by(username=username).first():
            flash('Пользователь с таким именем уже существует', 'error')
            return redirect(url_for('register'))
        if len(username) < 3 or len(password) < 4:
            flash('Имя > 2 символов, пароль > 3 символов', 'error')
            return redirect(url_for('register'))
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash('Регистрация успешна! Теперь войдите.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('index'))
        else:
            flash('Неверное имя или пароль', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# === Маршруты: Профиль ===
@app.route('/profile/<username>')
@login_required
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    return render_template('profile.html', profile_user=user)

@app.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect(url_for('logout'))
    if request.method == 'POST':
        bio = request.form.get('bio', '')
        if len(bio) <= 280:
            user.bio = bio
            db.session.commit()
            flash('Профиль обновлён!', 'success')
            notify_user_updated(user)
        else:
            flash('Описание слишком длинное (макс. 280 символов)', 'error')
    return render_template('edit_profile.html', user=user)

# === Маршруты: Загрузка файлов ===
@app.route('/upload/avatar', methods=['POST'])
@login_required
def upload_avatar():
    if 'avatar' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    file = request.files['avatar']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[1].lower()
        new_filename = f"{session['username']}_{int(datetime.now(timezone.utc).timestamp())}.{ext}"
        
        if CLOUDINARY_ENABLED:
            # Загрузка в Cloudinary
            upload_result = cloudinary.uploader.upload(file, folder='messenger/avatars', public_id=new_filename.rsplit('.', 1)[0])
            media_url = upload_result['secure_url']
            user = db.session.get(User, session['user_id'])
            if user:
                user.avatar_filename = new_filename  # Сохраняем имя для reference
                db.session.commit()
                notify_user_updated(user)
                return jsonify({'success': True, 'avatar_url': media_url})
        else:
            # Локальное сохранение
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], 'avatars')
            os.makedirs(save_path, exist_ok=True)
            file.save(os.path.join(save_path, new_filename))
            user = db.session.get(User, session['user_id'])
            if user:
                user.avatar_filename = new_filename
                db.session.commit()
                notify_user_updated(user)
                return jsonify({'success': True, 'avatar_url': user.avatar_url()})
    
    error_msg = 'Недопустимый формат файла'
    if file and file.filename:
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        error_msg = f'Формат .{ext} не поддерживается'
    return jsonify({'error': error_msg}), 400

@app.route('/upload/message', methods=['POST'])
@login_required
def upload_message_media():
    if 'media' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    file = request.files['media']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[1].lower()
        
        if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
            media_type = 'image'
            folder = 'images'
        elif ext in ['mp4', 'webm']:
            media_type = 'circle' if 'circle' in filename else 'video'
            folder = 'circles' if media_type == 'circle' else 'videos'
        else:
            media_type = 'file'
            folder = 'files'
        
        new_filename = f"{session['username']}_{int(datetime.now(timezone.utc).timestamp())}.{ext}"
        
        if CLOUDINARY_ENABLED:
            # Загрузка в Cloudinary
            upload_result = cloudinary.uploader.upload(file, folder=f'messenger/{folder}', public_id=new_filename.rsplit('.', 1)[0])
            media_url = upload_result['secure_url']
            return jsonify({
                'success': True,
                'media_url': media_url,  # Прямая ссылка на Cloudinary
                'media_type': media_type,
                'filename': filename
            })
        else:
            # Локальное сохранение
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], folder)
            os.makedirs(save_path, exist_ok=True)
            file.save(os.path.join(save_path, new_filename))
            return jsonify({
                'success': True,
                'media_url': url_for('static', filename=f'uploads/{folder}/{new_filename}'),
                'media_type': media_type,
                'filename': filename
            })
    return jsonify({'error': 'Недопустимый формат'}), 400

# === Маршруты: Группы ===
@app.route('/groups/create', methods=['GET', 'POST'])
@login_required
def create_group():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        member_ids = request.form.getlist('members')
        if not name:
            flash('Введите название группы', 'error')
            return redirect(url_for('create_group'))
        if len(member_ids) == 0:
            flash('Выберите хотя бы одного участника', 'error')
            return redirect(url_for('create_group'))
        group = ChatGroup(name=name, creator_id=session['user_id'])
        group.members.append(db.session.get(User, session['user_id']))
        for mid in member_ids:
            user = db.session.get(User, int(mid))
            if user:
                group.members.append(user)
        db.session.add(group)
        db.session.commit()
        flash(f'Группа "{name}" создана!', 'success')
        return redirect(url_for('index'))
    users = User.query.filter(User.id != session['user_id']).all()
    return render_template('create_group.html', users=users)

# === Маршруты: API ===
@app.route('/api/users')
@login_required
def get_users():
    users = User.query.filter(User.id != session['user_id']).all()
    return jsonify({'users': [u.to_dict() for u in users]})

@app.route('/api/groups')
@login_required
def get_groups():
    groups = ChatGroup.query.filter(ChatGroup.members.any(id=session['user_id'])).all()
    return jsonify({'groups': [g.to_dict() for g in groups]})

@app.route('/api/messages')
@login_required
def get_messages():
    messages = Message.query.filter_by(is_private=False, group_id=None).order_by(Message.timestamp.asc()).limit(100).all()
    return jsonify({'messages': [m.to_dict() for m in messages]})

@app.route('/api/messages/private/<int:user_id>')
@login_required
def get_private_messages(user_id):
    messages = Message.query.filter(
        ((Message.sender_id == session['user_id']) & (Message.recipient_id == user_id)) |
        ((Message.sender_id == user_id) & (Message.recipient_id == session['user_id']))
    ).filter(Message.group_id == None).order_by(Message.timestamp.asc()).limit(100).all()
    return jsonify({'messages': [m.to_dict() for m in messages]})

@app.route('/api/messages/group/<int:group_id>')
@login_required
def get_group_messages(group_id):
    group = ChatGroup.query.get(group_id)
    if not group or session['user_id'] not in [m.id for m in group.members]:
        return jsonify({'error': 'Нет доступа'}), 403
    messages = Message.query.filter_by(group_id=group_id).order_by(Message.timestamp.asc()).limit(100).all()
    return jsonify({'messages': [m.to_dict() for m in messages]})

# === WebSocket События ===
@socketio.on('connect')
def handle_connect():
    if 'user_id' not in session:
        disconnect()
        return False
    
    user = db.session.get(User, session['user_id'])
    if user:
        user.last_seen = datetime.now(timezone.utc)
        db.session.commit()
        online_users[user.id] = request.sid
        print(f"✅ {session.get('username')} подключился (sid: {request.sid})")
        emit('user_online', {'user_id': user.id}, broadcast=True)
        for uid in online_users.keys():
            if uid != user.id:
                emit('user_online', {'user_id': uid})
    
    join_room(f"user_{session['user_id']}")
    return True

@socketio.on('disconnect')
def handle_disconnect():
    if 'user_id' in session:
        user_id = session['user_id']
        username = session.get('username')
        if user_id in online_users:
            del online_users[user_id]
        print(f"❌ {username} отключился")
        emit('user_offline', {'user_id': user_id}, broadcast=True)

@socketio.on('join_private_chat')
def handle_join_private(data):
    if 'user_id' not in session:
        return
    join_room(f"user_{session['user_id']}")

@socketio.on('join_group')
def handle_join_group(data):
    if 'user_id' not in session:
        return
    group_id = data.get('group_id')
    if group_id:
        join_room(f"group_{group_id}")
        print(f"✅ {session['username']} присоединился к группе {group_id}")

@socketio.on('typing_start')
def handle_typing_start(data):
    if 'user_id' not in session:
        return
    recipient_id = data.get('recipient_id')
    if recipient_id:
        emit('user_typing', {'user_id': session['user_id'], 'username': session['username']}, room=f"user_{recipient_id}")

@socketio.on('typing_stop')
def handle_typing_stop(data):
    if 'user_id' not in session:
        return
    recipient_id = data.get('recipient_id')
    if recipient_id:
        emit('user_stop_typing', {'user_id': session['user_id']}, room=f"user_{recipient_id}")

@socketio.on('send_message')
def handle_message(data):
    if 'user_id' not in session:
        return
    text = data.get('text', '').strip() if data.get('text') else None
    media_url = data.get('media_url')
    media_type = data.get('media_type')
    recipient_id = data.get('recipient_id')
    group_id = data.get('group_id')
    if not text and not media_url:
        return
    media_filename = media_url.split('/')[-1] if media_url and not CLOUDINARY_ENABLED else media_url
    new_message = Message(
        sender_id=session['user_id'],
        recipient_id=recipient_id,
        group_id=group_id,
        text=text,
        media_filename=media_filename,
        media_type=media_type,
        is_private=(recipient_id is not None)
    )
    db.session.add(new_message)
    db.session.commit()
    msg_data = new_message.to_dict()
    if group_id:
        emit('receive_message', msg_data, room=f"group_{group_id}")
    elif recipient_id:
        emit('receive_message', msg_data, room=f"user_{session['user_id']}")
        emit('receive_message', msg_data, room=f"user_{recipient_id}")
        emit('user_stop_typing', {'user_id': session['user_id']}, room=f"user_{recipient_id}")
    else:
        emit('receive_message', msg_data, broadcast=True)

# === Запуск приложения ===
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("✅ Таблицы БД созданы/проверены")
    
    # Для Railway: используем переменную PORT от платформы
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1'
    
    print(f"🚀 Запуск сервера на {host}:{port}")
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)