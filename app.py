"""
Delivery System Flask Application
Standalone delivery, routing, and driver management system
"""
# Attempt early monkey-patching to enable greenlet-based websocket support.
# Must run before importing modules that create threads or use blocking locks
# (e.g., SQLAlchemy, werkzeug local objects). If unavailable we fall back.
ASYNC_MODE_OVERRIDE = None
_EVENTLET_AVAILABLE = False
_GEVENT_AVAILABLE = False
try:
    import eventlet  # type: ignore
    eventlet.monkey_patch()
    _EVENTLET_AVAILABLE = True
    ASYNC_MODE_OVERRIDE = 'eventlet'
except Exception:
    try:
        from gevent import monkey as gevent_monkey  # type: ignore
        gevent_monkey.patch_all()
        _GEVENT_AVAILABLE = True
        ASYNC_MODE_OVERRIDE = 'gevent'
    except Exception:
        ASYNC_MODE_OVERRIDE = None

import os
import logging
from datetime import timedelta

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, current_app
from flask_login import LoginManager, current_user, login_required, UserMixin
# from flask_migrate import Migrate  # Optional for manual migrations
from flask_cors import CORS
from flask_caching import Cache
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_wtf.csrf import CSRFProtect

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import local modules
from config import get_config
from database import db, init_db
from extensions import mail, cache
from models import Driver, Customer, Admin, Order, Delivery, DriverLocation


def create_app(config=None):
    """Application factory"""
    app = Flask(__name__, static_folder='static', static_url_path='/static', template_folder='templates')
    
    # Load configuration
    if config is None:
        config = get_config()
    app.config.from_object(config)
    
    logger.info(f"Creating app with config: {config.__class__.__name__}")
    
    # Create necessary directories
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    
    # Initialize extensions
    db.init_app(app)
    mail.init_app(app)
    cache.init_app(app)

    # Initialize database tables
    init_db(app)

    # NOTE: Disabled Google Drive download at startup to prevent memory issues
    # GraphML files should be included in the repo or downloaded manually
    # if os.environ.get('GOOGLE_DRIVE_ASHANTI_FILE_ID'):
    #     try:
    #         from utils.google_drive import download_graphml_files
    #         logger.info("Downloading GraphML files from Google Drive...")
    #         download_graphml_files()
    #     except Exception as e:
    #         logger.warning(f"Failed to download GraphML files from Google Drive: {e}")
    
    # Initialize CSRF Protection (init later so we can exempt socket.io)
    csrf = CSRFProtect()

    # Add context processor for csrf_token
    @app.context_processor
    def inject_csrf_token():
        from flask_wtf.csrf import generate_csrf
        return dict(csrf_token=generate_csrf)
    
    # Initialize Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'
    
    @login_manager.user_loader
    def load_user(user_id):
        """Load user from database"""
        from flask import session
        
        # Check session to determine user type
        user_type = session.get('user_type')
        
        if user_type == 'driver':
            return Driver.query.get(user_id)
        elif user_type == 'customer':
            return Customer.query.get(user_id)
        elif user_type == 'admin':
            return Admin.query.get(user_id)
        
        # Fallback: try to load as any type (for backwards compatibility)
        driver = Driver.query.get(user_id)
        if driver:
            return driver
        
        customer = Customer.query.get(user_id)
        if customer:
            return customer
        
        admin = Admin.query.get(user_id)
        if admin:
            return admin
        
        return None
    
    # Initialize Flask-Migrate (optional for manual migrations)
    # migrate = Migrate(app, db)
    
    # Initialize SocketIO — honor early-detected async mode
    async_mode = ASYNC_MODE_OVERRIDE if ASYNC_MODE_OVERRIDE else 'threading'
    if async_mode == 'eventlet':
        logger.info('SocketIO: eventlet available — enabling websocket support')
    elif async_mode == 'gevent':
        logger.info('SocketIO: gevent available — enabling websocket support')
    else:
        logger.warning('SocketIO: no eventlet/gevent detected — falling back to threading (websocket may not be supported)')

    # Enable CORS for Socket.IO (development mode allows all origins)
    socketio = SocketIO(
        app,
        cors=True,  # Allow all origins — development mode
        async_mode=async_mode,
        logger=app.debug,
        engineio_logger=app.debug
    )
    logger.info(f"SocketIO: CORS enabled for all origins")

    # Now initialize CSRF with the app and attempt to exempt engine.io WSGI handler
    csrf.init_app(app)
    # Try multiple possible handler attributes so exemption works across versions
    exemption_targets = []
    try:
        exemption_targets.append(('socketio.server.eio.handle_request', socketio.server.eio.handle_request))
    except Exception:
        pass
    try:
        exemption_targets.append(('socketio.server.eio.wsgi_app', socketio.server.eio.wsgi_app))
    except Exception:
        pass
    try:
        exemption_targets.append(('socketio.server.eio.app', socketio.server.eio.app))
    except Exception:
        pass

    exempted_any = False
    for name, target in exemption_targets:
        try:
            csrf.exempt(target)
            logger.info(f"CSRF: exempted engine.io handler -> {name}")
            exempted_any = True
        except Exception as e:
            logger.warning(f"CSRF: failed to exempt {name}: {e}")

    if not exempted_any:
        logger.warning("CSRF: no engine.io handler exempted; socket.io polling POSTs may be blocked")
    
    # CORS Configuration
    CORS(app, resources={r"/api/*": {"origins": app.config.get('CORS_ORIGINS', "*")}})
    
    # Register blueprints
    logger.info("Registering blueprints...")
    
    # Import blueprints (will create these next)
    try:
        from driver_routes import driver_bp
        from customer_routes import customer_bp
        from admin_routes import admin_bp
        from api_routes import api_bp
        
        app.register_blueprint(driver_bp)
        app.register_blueprint(customer_bp)
        app.register_blueprint(admin_bp)
        app.register_blueprint(api_bp)
        
        logger.info("Blueprints registered successfully")
    except ImportError as e:
        logger.warning(f"Some blueprints not available yet: {e}")
    
    # ===================== CONTEXT PROCESSORS =====================
    @app.context_processor
    def inject_user():
        """Inject user info into templates"""
        return dict(current_user=current_user)
    
    @app.context_processor
    def inject_config():
        """Inject config into templates"""
        return dict(config=app.config)
    
    # ===================== SOCKET IO EVENTS =====================
    
    # Store for driver Socket.IO connections (sid -> driver_id mapping)
    driver_socket_map = {}
    # Store for customer Socket.IO connections (sid -> customer_id mapping)
    customer_socket_map = {}
    
    @socketio.on('connect', namespace='/driver')
    def driver_connect(data=None, auth=None):
        """Handle driver connection"""
        from flask import request as flask_request
        sid = flask_request.sid
        
        logger.info(f"[SOCKETIO] Driver connection attempt - sid: {sid}, current_user: {current_user}, is Driver: {isinstance(current_user, Driver) if current_user else False}")
        
        if current_user and isinstance(current_user, Driver):
            logger.info(f"✓ [SOCKETIO] Driver {current_user.id} connected with session auth")
            room_name = f'driver_{current_user.id}'
            logger.info(f"[SOCKETIO] Driver joining room: {room_name}")
            join_room(room_name)
            driver_socket_map[sid] = current_user.id
            logger.info(f"[SOCKETIO] Driver socket map updated: {sid} -> {current_user.id}")
            emit('connected', {'message': 'Connected to server', 'driver_id': current_user.id})
        else:
            logger.warning(f"✗ [SOCKETIO] Driver connection rejected - no session auth. Will wait for auth event.")
            # Don't reject - drivers can authenticate via explicit event
    
    @socketio.on('disconnect', namespace='/driver')
    def driver_disconnect(data=None):
        """Handle driver disconnection"""
        from flask import request as flask_request
        sid = flask_request.sid
        
        if current_user and isinstance(current_user, Driver):
            logger.info(f"Driver {current_user.id} disconnected")
            leave_room(f'driver_{current_user.id}')
        
        # Clean up mapping
        if sid in driver_socket_map:
            del driver_socket_map[sid]
    
    @socketio.on('authenticate', namespace='/driver')
    def driver_authenticate(data):
        """Explicit driver authentication event (fallback if session auth failed)"""
        from flask import request as flask_request
        sid = flask_request.sid
        
        driver_id = data.get('driver_id')
        logger.info(f"[SOCKETIO] Driver authentication event - sid: {sid}, driver_id: {driver_id}, current_user: {current_user}")
        
        # Verify we have current_user
        if current_user and isinstance(current_user, Driver):
            if current_user.id == driver_id:
                logger.info(f"✓ [SOCKETIO] Driver {driver_id} authenticated via event")
                join_room(f'driver_{driver_id}')
                driver_socket_map[sid] = driver_id
                emit('authenticated', {'status': 'ok', 'driver_id': driver_id})
            else:
                logger.warning(f"✗ [SOCKETIO] Driver {driver_id} auth failed - mismatched ID (current: {current_user.id})")
                emit('authenticated', {'status': 'error', 'reason': 'ID mismatch'})
        else:
            logger.warning(f"✗ [SOCKETIO] Driver {driver_id} auth failed - not authenticated")
            emit('authenticated', {'status': 'error', 'reason': 'Not authenticated'})

    
    @socketio.on('location_update', namespace='/driver')
    def handle_location_update(data):
        """Handle driver location updates"""
        if current_user and isinstance(current_user, Driver):
            try:
                lat = float(data.get('lat'))
                lng = float(data.get('lng'))
                delivery_id = data.get('delivery_id')
                
                # Update driver location
                loc = DriverLocation.query.filter_by(driver_id=current_user.id).first()
                if not loc:
                    loc = DriverLocation(driver_id=current_user.id)
                
                loc.latitude = lat
                loc.longitude = lng
                loc.delivery_id = delivery_id
                
                db.session.add(loc)
                db.session.commit()
                
                # Broadcast to monitors
                emit('driver_updated', {
                    'driver_id': current_user.id,
                    'driver_name': current_user.full_name,
                    'lat': lat,
                    'lng': lng,
                    'delivery_id': delivery_id
                }, broadcast=True, namespace='/monitor')
                
                logger.debug(f"Updated location for driver {current_user.id}")
            except Exception as e:
                logger.error(f"Error updating driver location: {e}")
                emit('error', {'message': str(e)})
    
    @socketio.on('connect', namespace='/monitor')
    def monitor_connect(data=None, auth=None):
        """Handle monitor/dispatcher connection"""
        from flask import request as flask_request
        sid = flask_request.sid
        logger.info(f"Monitor connected: {sid}")
        join_room('monitor_room')
        emit('connected', {'message': 'Connected to monitor room'})

    @socketio.on('connect', namespace='/customer')
    def customer_connect(data=None, auth=None):
        """Handle customer socket connections for in-app events"""
        from flask import request as flask_request
        sid = flask_request.sid
        
        # current_user will be available if session cookie is sent
        logger.info(f"[SOCKETIO] Customer connection attempt - sid: {sid}, current_user: {current_user}, is Customer: {isinstance(current_user, Customer) if current_user else False}")
        
        if current_user and isinstance(current_user, Customer):
            logger.info(f"[SOCKETIO] Customer {current_user.id} connected via Socket.IO with session auth")
            room_name = f'customer_{current_user.id}'
            logger.info(f"[SOCKETIO] Joining room: {room_name}")
            join_room(room_name)
            customer_socket_map[sid] = current_user.id
            logger.info(f"[SOCKETIO] Customer socket map updated: {sid} -> {current_user.id}")
            emit('connected', {'message': 'Connected to customer namespace', 'customer_id': current_user.id})
        else:
            logger.warning(f"[SOCKETIO] Customer connection - no session auth. Will wait for auth event.")
            # Don't reject - allow but require authentication event
    
    @socketio.on('disconnect', namespace='/customer')
    def customer_disconnect(data=None):
        """Handle customer disconnection"""
        from flask import request as flask_request
        sid = flask_request.sid
        
        if current_user and isinstance(current_user, Customer):
            logger.info(f"Customer {current_user.id} disconnected from Socket.IO")
            leave_room(f'customer_{current_user.id}')
        
        # Clean up mapping
        if sid in customer_socket_map:
            del customer_socket_map[sid]
    
    @socketio.on('authenticate', namespace='/customer')
    def customer_authenticate(data):
        """Explicit customer authentication event"""
        from flask import request as flask_request
        sid = flask_request.sid
        
        customer_id = data.get('customer_id')
        logger.info(f"[SOCKETIO] Customer authentication event - sid: {sid}, customer_id: {customer_id}, current_user: {current_user}")
        
        if current_user and isinstance(current_user, Customer):
            if current_user.id == customer_id:
                logger.info(f"✓ [SOCKETIO] Customer {customer_id} authenticated via event")
                join_room(f'customer_{customer_id}')
                customer_socket_map[sid] = customer_id
                emit('authenticated', {'status': 'ok', 'customer_id': customer_id})
            else:
                logger.warning(f"✗ [SOCKETIO] Customer {customer_id} auth failed - mismatched ID (current: {current_user.id})")
                emit('authenticated', {'status': 'error', 'reason': 'ID mismatch'})
        else:
            logger.warning(f"✗ [SOCKETIO] Customer {customer_id} auth failed - not authenticated")
            emit('authenticated', {'status': 'error', 'reason': 'Not authenticated'})

    # ─── CALL EVENTS ───────────────────────────────────
    @socketio.on('call_initiate', namespace='/customer')
    def handle_call_initiate(data):
        """Customer initiates a call to driver"""
        if not current_user or not isinstance(current_user, Customer):
            logger.warning(f"[CALL] Unauthorized call_initiate - current_user: {current_user}")
            emit('error', {'message': 'Unauthorized'})
            return
        
        driver_id = data.get('driver_id')
        delivery_id = data.get('delivery_id')
        
        if not driver_id or not delivery_id:
            logger.warning(f"[CALL] Missing call data - driver_id: {driver_id}, delivery_id: {delivery_id}")
            emit('error', {'message': 'Missing driver_id or delivery_id'})
            return
        
        logger.info(f"[CALL] ✓ Customer {current_user.id} initiating call to driver {driver_id} for delivery {delivery_id}")
        logger.info(f"[CALL] Customer name field: '{current_user.name}', email field: '{current_user.email}'")

        # Use name if available, otherwise extract from email
        if current_user.name:
            customer_display_name = current_user.name
        elif current_user.email:
            # Extract name from email (e.g., "takyimildred4" from "takyimildred4@gmail.com")
            email_name = current_user.email.split('@')[0]
            # Remove numbers and capitalize
            import re
            customer_display_name = re.sub(r'\d+', '', email_name).title()
        else:
            customer_display_name = f'Customer #{current_user.id}'
        
        logger.info(f"[CALL] Using customer display name: '{customer_display_name}'")

        call_payload = {
            'customer_id': current_user.id,
            'customer_name': customer_display_name,
            'delivery_id': delivery_id,
            'call_id': f"{current_user.id}_{driver_id}_{delivery_id}_{int(__import__('time').time())}"
        }

        # Send call notification to driver
        logger.info(f"[CALL] Emitting 'call_incoming' to room driver_{driver_id} namespace /driver")
        logger.info(f"[CALL] Call payload: {call_payload}")
        result = socketio.emit('call_incoming', call_payload, room=f'driver_{driver_id}', namespace='/driver')
        logger.info(f"[CALL] Emit result: {result}")

        # Diagnostic: check whether the driver room had participants (helpful when debugging missed deliveries)
        participants = []
        try:
            mgr = socketio.server.manager
            if hasattr(mgr, 'get_participants'):
                participants = list(mgr.get_participants('/driver', f'driver_{driver_id}'))
            else:
                # Fallback for managers exposing rooms mapping
                rooms = getattr(mgr, 'rooms', None)
                if rooms and isinstance(rooms, dict):
                    ns_rooms = rooms.get('/driver') or rooms.get('driver') or {}
                    participants = list(ns_rooms.get(f'driver_{driver_id}', []))
        except Exception as e:
            logger.debug(f"[CALL] Could not inspect room participants: {e}")

        logger.info(f"[CALL] Driver room driver_{driver_id} participants: {len(participants)}")
        
        # Check driver_socket_map as backup
        mapped_driver_sids = [sid for sid, did in driver_socket_map.items() if did == driver_id]
        logger.info(f"[CALL] Driver {driver_id} socket map check: {len(mapped_driver_sids)} connections")
        logger.info(f"[CALL] Current driver_socket_map: {driver_socket_map}")
        
        # Notify caller via return value (ack) and optional event
        if len(participants) == 0 and len(mapped_driver_sids) == 0:
            # Notify customer that the driver wasn't available to receive the call
            logger.warning(f"[CALL] Driver {driver_id} not in any room - likely offline")
            try:
                emit('call_failed', {'reason': 'Driver not connected or unavailable', 'call_id': call_payload.get('call_id')})
            except Exception:
                logger.debug('[CALL] Failed to emit call_failed to customer')

        # Return ack information to the emitter (if client provided a callback)
        return {
            'status': 'ok',
            'call_id': call_payload.get('call_id'),
            'driver_online': len(participants) > 0 or len(mapped_driver_sids) > 0,
            'participants': len(participants),
            'mapped_sids': len(mapped_driver_sids)
        }
    
    @socketio.on('call_accept', namespace='/driver')
    def handle_call_accept(data):
        """Driver accepts incoming call"""
        if not current_user or not isinstance(current_user, Driver):
            emit('error', {'message': 'Unauthorized'})
            return
        
        customer_id = data.get('customer_id')
        delivery_id = data.get('delivery_id')
        call_id = data.get('call_id')
        
        logger.info(f"Driver {current_user.id} accepted call from customer {customer_id}")
        
        # Notify customer that call was accepted
        logger.debug(f"Emitting 'call_accepted' to room customer_{customer_id} namespace /customer")
        socketio.emit('call_accepted', {
            'driver_id': current_user.id,
            'driver_name': current_user.full_name,
            'call_id': call_id
        }, room=f'customer_{customer_id}', namespace='/customer')
    
    @socketio.on('call_reject', namespace='/driver')
    def handle_call_reject(data):
        """Driver rejects incoming call"""
        if not current_user or not isinstance(current_user, Driver):
            emit('error', {'message': 'Unauthorized'})
            return
        
        customer_id = data.get('customer_id')
        call_id = data.get('call_id')
        
        logger.info(f"Driver {current_user.id} rejected call from customer {customer_id}")
        
        # Notify customer that call was rejected
        logger.debug(f"Emitting 'call_rejected' to room customer_{customer_id} namespace /customer")
        socketio.emit('call_rejected', {
            'call_id': call_id,
            'reason': 'Driver declined'
        }, room=f'customer_{customer_id}', namespace='/customer')
    
    @socketio.on('call_end', namespace='/customer')
    def handle_call_end_customer(data):
        """Customer ends call"""
        if not current_user or not isinstance(current_user, Customer):
            emit('error', {'message': 'Unauthorized'})
            return
        
        driver_id = data.get('driver_id')
        delivery_id = data.get('delivery_id')
        call_id = data.get('call_id')
        
        logger.info(f"Customer {current_user.id} ended call with driver {driver_id}")
        
        # Notify driver
        socketio.emit('call_ended', {
            'call_id': call_id,
            'customer_id': current_user.id,
            'delivery_id': delivery_id,
            'ended_by': 'customer'
        }, room=f'driver_{driver_id}', namespace='/driver')
    
    @socketio.on('call_accept_driver_call', namespace='/customer')
    def handle_call_accept_driver_call(data):
        """Customer accepts incoming call from driver"""
        if not current_user or not isinstance(current_user, Customer):
            emit('error', {'message': 'Unauthorized'})
            return
        
        driver_id = data.get('driver_id')
        delivery_id = data.get('delivery_id')
        call_id = data.get('call_id')
        
        logger.info(f"Customer {current_user.id} accepted incoming call from driver {driver_id}")
        
        # Notify driver that customer accepted
        logger.debug(f"Emitting 'call_accepted_by_customer' to room driver_{driver_id} namespace /driver")
        socketio.emit('call_accepted_by_customer', {
            'driver_id': driver_id,
            'call_id': call_id
        }, room=f'driver_{driver_id}', namespace='/driver')
    
    @socketio.on('call_end', namespace='/driver')
    def handle_call_end_driver(data):
        """Driver ends call"""
        if not current_user or not isinstance(current_user, Driver):
            emit('error', {'message': 'Unauthorized'})
            return
        
        customer_id = data.get('customer_id')
        call_id = data.get('call_id')
        
        logger.info(f"Driver {current_user.id} ended call with customer {customer_id}")
        
        # Notify customer
        socketio.emit('call_ended', {
            'call_id': call_id,
            'ended_by': 'driver'
        }, room=f'customer_{customer_id}', namespace='/customer')
    
    @socketio.on('call_initiate', namespace='/driver')
    def handle_driver_call_initiate(data):
        """Driver initiates a call to customer"""
        if not current_user or not isinstance(current_user, Driver):
            emit('error', {'message': 'Unauthorized'})
            return
        
        customer_id = data.get('customer_id')
        delivery_id = data.get('delivery_id')
        
        if not customer_id or not delivery_id:
            emit('error', {'message': 'Missing customer_id or delivery_id'})
            return
        
        logger.info(f"[CALL] Driver {current_user.id} initiating call to customer {customer_id} for delivery {delivery_id}")

        call_payload = {
            'driver_id': current_user.id,
            'driver_name': current_user.full_name if current_user.full_name else current_user.email,
            'delivery_id': delivery_id,
            'call_id': f"{current_user.id}_{customer_id}_{delivery_id}_{int(__import__('time').time())}"
        }

        logger.info(f"[CALL] Call payload: {call_payload}")

        # Send call notification to customer
        logger.info(f"[CALL] Emitting 'driver_call_incoming' to room customer_{customer_id} namespace /customer")
        result = socketio.emit('driver_call_incoming', call_payload, room=f'customer_{customer_id}', namespace='/customer')
        logger.info(f"[CALL] Emit result: {result}")

        # Diagnostic: check whether the customer room had participants
        participants = []
        try:
            mgr = socketio.server.manager
            if hasattr(mgr, 'get_participants'):
                participants = list(mgr.get_participants('/customer', f'customer_{customer_id}'))
            else:
                rooms = getattr(mgr, 'rooms', None)
                if rooms and isinstance(rooms, dict):
                    ns_rooms = rooms.get('/customer') or rooms.get('customer') or {}
                    participants = list(ns_rooms.get(f'customer_{customer_id}', []))
        except Exception as e:
            logger.debug(f"[CALL] Could not inspect room participants: {e}")

        logger.info(f"[CALL] Customer room customer_{customer_id} participants: {len(participants)}")
        
        # Check customer_socket_map as backup
        mapped_customer_sids = [sid for sid, cid in customer_socket_map.items() if cid == customer_id]
        logger.info(f"[CALL] Customer {customer_id} socket map check: {len(mapped_customer_sids)} connections")
        logger.info(f"[CALL] Current customer_socket_map: {customer_socket_map}")
        
        if len(participants) == 0 and len(mapped_customer_sids) == 0:
            logger.warning(f"[CALL] Customer {customer_id} not in any room - likely offline")
            try:
                emit('call_failed', {'reason': 'Customer not connected or unavailable', 'call_id': call_payload.get('call_id')}, namespace='/driver')
            except Exception:
                logger.debug('Failed to emit call_failed to driver')
        # Return ack for driver emitter
        return {
            'status': 'ok',
            'call_id': call_payload.get('call_id'),
            'customer_online': len(participants) > 0,
            'participants': len(participants)
        }

    # ===================== WEBRTC SIGNALING =====================

    @socketio.on('webrtc_offer', namespace='/customer')
    def handle_webrtc_offer_customer(data):
        """Customer sends WebRTC offer to driver"""
        if not current_user or not isinstance(current_user, Customer):
            emit('error', {'message': 'Unauthorized'})
            return

        call_id = data.get('call_id')
        offer = data.get('offer')

        logger.info(f"[WEBRTC] Customer {current_user.id} sending offer for call {call_id}")

        # Extract driver_id from call_id (format: driver_customer_delivery_timestamp)
        if call_id:
            parts = call_id.split('_')
            if len(parts) >= 1:
                driver_id = int(parts[0])

                # Relay offer to driver
                socketio.emit('webrtc_offer', {
                    'call_id': call_id,
                    'offer': offer
                }, room=f'driver_{driver_id}', namespace='/driver')
                logger.info(f"[WEBRTC] Offer relayed to driver {driver_id}")

    @socketio.on('webrtc_offer', namespace='/driver')
    def handle_webrtc_offer_driver(data):
        """Driver sends WebRTC offer to customer"""
        if not current_user or not isinstance(current_user, Driver):
            emit('error', {'message': 'Unauthorized'})
            return

        call_id = data.get('call_id')
        offer = data.get('offer')

        logger.info(f"[WEBRTC] Driver {current_user.id} sending offer for call {call_id}")

        # Extract customer_id from call_id (format: driver_customer_delivery_timestamp)
        if call_id:
            parts = call_id.split('_')
            if len(parts) >= 2:
                customer_id = int(parts[1])

                # Relay offer to customer
                socketio.emit('webrtc_offer', {
                    'call_id': call_id,
                    'offer': offer
                }, room=f'customer_{customer_id}', namespace='/customer')
                logger.info(f"[WEBRTC] Offer relayed to customer {customer_id}")

    @socketio.on('webrtc_answer', namespace='/customer')
    def handle_webrtc_answer_customer(data):
        """Customer sends WebRTC answer to driver"""
        if not current_user or not isinstance(current_user, Customer):
            emit('error', {'message': 'Unauthorized'})
            return

        call_id = data.get('call_id')
        answer = data.get('answer')

        logger.info(f"[WEBRTC] Customer {current_user.id} sending answer for call {call_id}")

        # Extract driver_id from call_id
        if call_id:
            parts = call_id.split('_')
            if len(parts) >= 1:
                driver_id = int(parts[0])

                # Relay answer to driver
                socketio.emit('webrtc_answer', {
                    'call_id': call_id,
                    'answer': answer
                }, room=f'driver_{driver_id}', namespace='/driver')
                logger.info(f"[WEBRTC] Answer relayed to driver {driver_id}")

    @socketio.on('webrtc_answer', namespace='/driver')
    def handle_webrtc_answer_driver(data):
        """Driver sends WebRTC answer to customer"""
        if not current_user or not isinstance(current_user, Driver):
            emit('error', {'message': 'Unauthorized'})
            return

        call_id = data.get('call_id')
        answer = data.get('answer')

        logger.info(f"[WEBRTC] Driver {current_user.id} sending answer for call {call_id}")

        # Extract customer_id from call_id
        if call_id:
            parts = call_id.split('_')
            if len(parts) >= 2:
                customer_id = int(parts[1])

                # Relay answer to customer
                socketio.emit('webrtc_answer', {
                    'call_id': call_id,
                    'answer': answer
                }, room=f'customer_{customer_id}', namespace='/customer')
                logger.info(f"[WEBRTC] Answer relayed to customer {customer_id}")

    @socketio.on('webrtc_ice_candidate', namespace='/customer')
    def handle_webrtc_ice_candidate_customer(data):
        """Customer sends ICE candidate to driver"""
        if not current_user or not isinstance(current_user, Customer):
            emit('error', {'message': 'Unauthorized'})
            return

        call_id = data.get('call_id')
        candidate = data.get('candidate')

        logger.info(f"[WEBRTC] Customer {current_user.id} sending ICE candidate for call {call_id}")

        # Extract driver_id from call_id
        if call_id:
            parts = call_id.split('_')
            if len(parts) >= 1:
                driver_id = int(parts[0])

                # Relay ICE candidate to driver
                socketio.emit('webrtc_ice_candidate', {
                    'call_id': call_id,
                    'candidate': candidate
                }, room=f'driver_{driver_id}', namespace='/driver')
                logger.info(f"[WEBRTC] ICE candidate relayed to driver {driver_id}")

    @socketio.on('webrtc_ice_candidate', namespace='/driver')
    def handle_webrtc_ice_candidate_driver(data):
        """Driver sends ICE candidate to customer"""
        if not current_user or not isinstance(current_user, Driver):
            emit('error', {'message': 'Unauthorized'})
            return

        call_id = data.get('call_id')
        candidate = data.get('candidate')

        logger.info(f"[WEBRTC] Driver {current_user.id} sending ICE candidate for call {call_id}")

        # Extract customer_id from call_id
        if call_id:
            parts = call_id.split('_')
            if len(parts) >= 2:
                customer_id = int(parts[1])

                # Relay ICE candidate to customer
                socketio.emit('webrtc_ice_candidate', {
                    'call_id': call_id,
                    'candidate': candidate
                }, room=f'customer_{customer_id}', namespace='/customer')
                logger.info(f"[WEBRTC] ICE candidate relayed to customer {customer_id}")

    # ===================== ERROR HANDLERS =====================
    
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({'error': 'Not found'}), 404
    
    @app.errorhandler(403)
    def forbidden(error):
        return jsonify({'error': 'Forbidden'}), 403
    
    @app.errorhandler(500)
    def internal_error(error):
        logger.error(f"Internal server error: {error}")
        return jsonify({'error': 'Internal server error'}), 500
    
    # ===================== CLI COMMANDS =====================
    
    @app.cli.command()
    def init_database():
        """Initialize the database"""
        init_db(app)
        logger.info("Database initialized")
    
    @app.cli.command()
    def create_admin():
        """Create an admin user"""
        import click
        username = click.prompt('Admin username')
        email = click.prompt('Admin email')
        password = click.prompt('Admin password', hide_input=True, confirmation_prompt=True)
        
        admin = Admin(username=username, email=email)
        admin.set_password(password)
        admin.can_manage_drivers = True
        admin.can_view_analytics = True
        admin.can_manage_orders = True
        admin.can_manage_admins = True
        
        db.session.add(admin)
        db.session.commit()
        logger.info(f"Admin {username} created successfully")
    
    # ===================== HOME ROUTE =====================
    
    @app.route('/conversation/<int:conv_id>', methods=['GET'])
    @login_required
    def unified_conversation(conv_id):
        """Unified conversation page accessible to both customer and driver"""
        from models import Conversation, Delivery
        from sqlalchemy.orm import joinedload
        
        conv = Conversation.query.get_or_404(conv_id)
        
        # Load delivery with order and customer relationships
        if conv.delivery_id:
            delivery = Delivery.query.options(
                joinedload(Delivery.order).joinedload('customer'),
                joinedload(Delivery.driver)
            ).get(conv.delivery_id)
        else:
            delivery = None
        
        # Check access: customer or driver only
        user_type = None
        if isinstance(current_user, Customer):
            if conv.customer_id != current_user.id:
                return jsonify({'error': 'Unauthorized'}), 403
            user_type = 'customer'
        elif isinstance(current_user, Driver):
            if conv.driver_id != current_user.id:
                return jsonify({'error': 'Unauthorized'}), 403
            user_type = 'driver'
        else:
            return redirect(url_for('login'))
        
        if not user_type:
            return redirect(url_for('login'))
        
        return render_template('conversation_unified.html', conv=conv, delivery=delivery, user_type=user_type)
    
    @app.route('/')
    def index():
        """Home page"""
        return render_template('index.html')
    
    @app.route('/uploads/drivers/documents/<filename>')
    def uploaded_file(filename):
        """Serve uploaded driver documents"""
        from flask import send_from_directory
        uploads_dir = os.path.join(current_app.root_path, 'uploads', 'drivers', 'documents')
        return send_from_directory(uploads_dir, filename)
    
    @app.route('/login')
    def login():
        """Generic login dispatcher - redirects to appropriate login page"""
        user_type = request.args.get('type', '').lower()
        if user_type == 'driver':
            return redirect(url_for('driver.login'))
        elif user_type == 'customer':
            return redirect(url_for('customer.login'))
        elif user_type == 'admin':
            return redirect(url_for('admin.login'))
        # Default: redirect to customer login if no type specified
        return redirect(url_for('customer.login'))
    
    @app.route('/health')
    def health():
        """Health check endpoint"""
        return jsonify({'status': 'healthy', 'version': '1.0.0'}), 200
    
    @app.route('/route', methods=['POST'])
    def calculate_route():
        """Calculate route between two points using actual road networks"""
        try:
            data = request.get_json()
            if not data:
                logger.error("No JSON data received in /route request")
                return jsonify({'error': 'No JSON data provided'}), 400
            
            # Extract and validate coordinates
            try:
                pickup_lat = float(data.get('pickup_lat'))
                pickup_lng = float(data.get('pickup_lng'))
                dropoff_lat = float(data.get('dropoff_lat'))
                dropoff_lng = float(data.get('dropoff_lng'))
            except (TypeError, ValueError) as e:
                logger.error(f"Invalid coordinate format: {e}")
                return jsonify({'error': f'Invalid coordinates: {str(e)}'}), 400
            
            driver_region = data.get('driver_region', 'Northern')
            logger.info(f"[ROUTE] Calculating route: ({pickup_lat}, {pickup_lng}) -> ({dropoff_lat}, {dropoff_lng}) in region {driver_region}")
            
            # Use actual road-based routing from graphml
            from graphml import get_route_on_roads
            
            pickup = {'lat': pickup_lat, 'lng': pickup_lng}
            dropoff = {'lat': dropoff_lat, 'lng': dropoff_lng}
            
            # Get actual road-based route
            logger.info(f"[ROUTE] Calling get_route_on_roads with pickup={pickup}, dropoff={dropoff}")
            route_data = get_route_on_roads(
                pickup=pickup,
                dropoff=dropoff,
                num_alternatives=1,
                detail_level='medium',
                region=driver_region
            )
            
            logger.info(f"[ROUTE] Route data received: {route_data}")
            
            # Calculate distance and duration from route coordinates
            import math
            
            def haversine_distance(lat1, lng1, lat2, lng2):
                """Calculate distance in km between two coordinates"""
                R = 6371  # Earth's radius in km
                
                lat1_rad = math.radians(lat1)
                lat2_rad = math.radians(lat2)
                delta_lat = math.radians(lat2 - lat1)
                delta_lng = math.radians(lng2 - lng1)
                
                a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng/2)**2
                c = 2 * math.asin(math.sqrt(a))
                
                return R * c
            
            # For better estimate: calculate total distance from route coordinates if available
            route_coords = route_data.get('route_coords', [[pickup_lng, pickup_lat], [dropoff_lng, dropoff_lat]])
            
            logger.info(f"[ROUTE] Route has {len(route_coords)} waypoints")
            
            total_distance = 0
            for i in range(len(route_coords) - 1):
                lng1, lat1 = route_coords[i]
                lng2, lat2 = route_coords[i + 1]
                total_distance += haversine_distance(lat1, lng1, lat2, lng2)
            
            # Estimate duration: assume average speed of 25 km/h (Ghana traffic conditions)
            # Add 10% buffer for traffic
            estimated_speed = 25  # km/h (conservative)
            base_duration = (total_distance / estimated_speed) * 60  # minutes
            duration_minutes = base_duration * 1.1
            
            # Response format matching create_order.html expectations
            response = {
                'route_coords': route_coords,  # Top-level route coords for frontend
                'distance_km': round(total_distance, 2),
                'duration_minutes': round(duration_minutes, 1),
                'region': driver_region,
                'primary': {
                    'coordinates': route_coords  # Keep for backward compatibility
                },
                'alternatives': []
            }
            
            logger.info(f"[ROUTE] ✓ Route calculated: {total_distance:.2f}km, {duration_minutes:.1f}min with {len(route_coords)} waypoints")
            return jsonify(response), 200
            
        except Exception as e:
            logger.error(f"[ROUTE] ✗ Route calculation error: {type(e).__name__}: {str(e)}", exc_info=True)
            return jsonify({'error': f'{type(e).__name__}: {str(e)}'}), 400
    
    # Store socketio on app for use in blueprints
    app.socketio = socketio
    
    return app


# Create the app instance for WSGI servers
app = create_app()

if __name__ == '__main__':
    from driver_routes import driver_bp
    from customer_routes import customer_bp
    from admin_routes import admin_bp
    from api_routes import api_bp
    
    # Create database tables
    with app.app_context():
        db.create_all()
        logger.info("Database tables created")
        
        # Create default admin if none exists
        admin_exists = Admin.query.filter_by(email='niiodartei24@gmail.com').first()
        if not admin_exists:
            default_admin = Admin(
                username='admin',
                email='niiodartei24@gmail.com'
            )
            default_admin.set_password('odartei')
            default_admin.can_manage_drivers = True
            default_admin.can_view_analytics = True
            default_admin.can_manage_orders = True
            default_admin.can_manage_admins = True
            default_admin.is_active = True
            
            db.session.add(default_admin)
            db.session.commit()
            logger.info("Default admin account created: niiodartei24@gmail.com")
    
    # Run the app with SocketIO
    app.socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=True,
        use_reloader=True
    )
