"""
Driver Routes - Essential Functions Only
Driver authentication, dashboard, profile, and basic functionality
"""
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from werkzeug.utils import secure_filename

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import login_required, current_user, logout_user
from werkzeug.security import generate_password_hash, check_password_hash

from database import db
from models import Driver, Delivery, Order, Customer, DriverRating, Wallet, WalletTransaction, DriverLocation, DeliveryRequest, DriverOnboarding, Document, PaymentStatement, RatingFeedback, DriverEarningsMetric, Conversation, Message, CallLog
from forms import DriverLoginForm, DriverRegistrationForm

logger = logging.getLogger(__name__)
driver_bp = Blueprint('driver', __name__, url_prefix='/driver')

# ===================== AUTHENTICATION =====================

@driver_bp.route('/register', methods=['GET', 'POST'])
def register():
    """Driver registration - Multi-step process"""
    if request.method == 'POST':
        step = request.form.get('step', '1')
        
        if step == '1':
            return _register_step1()
        elif step == '2':
            return _register_step2()
    
    return render_template('driver/register.html')


def _register_step1():
    """Step 1: Basic driver information validation"""
    from flask import session
    
    data = request.form
    
    # Validate inputs
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()
    full_name = data.get('full_name', '').strip()
    license_number = data.get('license_number', '').strip()
    region = data.get('region', '').strip()
    vehicle_type = data.get('vehicle_type', '').strip()
    vehicle_number = data.get('vehicle_number', '').strip()
    service_type = data.get('service_type', '').strip()
    password = data.get('password', '')
    confirm_password = data.get('confirm_password', '')
    
    # Validation
    if not all([email, phone, full_name, license_number, region, vehicle_type, vehicle_number, service_type, password]):
        flash('All fields are required', 'danger')
        return redirect(url_for('driver.register'))
    
    if password != confirm_password:
        flash('Passwords do not match', 'danger')
        return redirect(url_for('driver.register'))
    
    if len(password) < 8:
        flash('Password must be at least 8 characters', 'danger')
        return redirect(url_for('driver.register'))
    
    # Validate vehicle type restrictions by region
    # Praggia is now available in all regions
    # if vehicle_type == 'praggia' and region != 'Ashanti':
    #     return jsonify({
    #         'success': False,
    #         'message': 'Praggia delivery is only available in Ashanti region'
    #     }), 400
    
    # Validate service type restrictions by vehicle type
    restricted_vehicles = ['bicycle', 'abobo_yaa', 'truck']
    if vehicle_type in restricted_vehicles and service_type != 'package_delivery':
        return jsonify({
            'success': False,
            'message': f'{vehicle_type.title()} can only provide package delivery service'
        }), 400
    
    # Check if driver already exists
    if Driver.query.filter_by(email=email).first():
        flash('Email already registered', 'danger')
        return redirect(url_for('driver.register'))
    
    # Store basic info in session for step 2
    session['registration_data'] = {
        'email': email,
        'phone': phone,
        'full_name': full_name,
        'license_number': license_number,
        'region': region,
        'vehicle_type': vehicle_type,
        'vehicle_number': vehicle_number,
        'service_type': service_type,
        'password': password
    }
    session.modified = True
    
    # Return JSON response for AJAX request
    return jsonify({
        'success': True,
        'message': 'Information saved successfully'
    })


def _register_step2():
    """Step 2: Document uploads and driver account creation"""
    from flask import session
    
    # Retrieve basic info from session
    reg_data = session.get('registration_data')
    if not reg_data:
        flash('Session expired. Please start registration again.', 'danger')
        return redirect(url_for('driver.register'))
    
    try:
        # Validate required document uploads
        required_docs = {
            'profile_pic': 'Profile Picture',
            'drivers_license': "Driver's License",
            'insurance_cert': 'Vehicle Insurance Certificate',
            'roadworthy_cert': 'Roadworthiness Certificate',
            'ghana_card': 'Ghana Card'
        }
        
        uploaded_files = {}
        for doc_key, doc_name in required_docs.items():
            if doc_key not in request.files or not request.files[doc_key].filename:
                flash(f'{doc_name} is required', 'danger')
                return redirect(url_for('driver.register'))
            
            file = request.files[doc_key]
            if not _allowed_file(file.filename):
                flash(f'{doc_name} must be JPG, PNG, or PDF', 'danger')
                return redirect(url_for('driver.register'))
            
            uploaded_files[doc_key] = file
        
        # Check if driver already exists by email or license number
        existing_driver_email = Driver.query.filter_by(email=reg_data['email']).first()
        if existing_driver_email:
            flash('Email already registered', 'danger')
            return redirect(url_for('driver.register'))
        
        existing_driver_license = Driver.query.filter_by(license_number=reg_data['license_number']).first()
        if existing_driver_license:
            flash('License number already registered', 'danger')
            return redirect(url_for('driver.register'))
        
        # Create upload directory if needed
        upload_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'drivers', 'documents')
        os.makedirs(upload_dir, exist_ok=True)
        
        # Create driver account
        driver = Driver(
            email=reg_data['email'],
            phone=reg_data['phone'],
            full_name=reg_data['full_name'],
            license_number=reg_data['license_number'],
            region=reg_data['region'],
            vehicle_type=reg_data['vehicle_type'],
            vehicle_number=reg_data['vehicle_number'],
            service_types=reg_data['service_type'],
            status='Pending'  # Pending document verification
        )
        driver.set_password(reg_data['password'])
        
        db.session.add(driver)
        db.session.flush()  # Get driver ID without committing
        
        # Create onboarding record
        onboarding = DriverOnboarding(
            driver_id=driver.id,
            status='Pending',
            approval_stage=1
        )
        db.session.add(onboarding)
        db.session.flush()  # Get onboarding ID
        
        # Upload and store documents
        document_mapping = {
            'profile_pic': 'Profile Picture',
            'drivers_license': 'Driver License',
            'insurance_cert': 'Vehicle Insurance',
            'roadworthy_cert': 'Roadworthiness Certificate',
            'ghana_card': 'Ghana Card'  # Optional
        }
        
        for file_key, doc_type in document_mapping.items():
            if file_key in uploaded_files:
                file = uploaded_files[file_key]
                filename = secure_filename(f"driver_{driver.id}_{file_key}_{datetime.utcnow().timestamp()}.{file.filename.rsplit('.', 1)[1] if '.' in file.filename else 'jpg'}")
                filepath = os.path.join(upload_dir, filename)
                file.save(filepath)
                
                # Create document record
                document = Document(
                    onboarding_id=onboarding.id,
                    document_type=file_key,
                    file_path=os.path.join('drivers', 'documents', filename),
                    verification_status='Pending'
                )
                db.session.add(document)
        
        # Create wallet for driver
        wallet = Wallet(
            driver_id=driver.id,
            balance=0
        )
        db.session.add(wallet)
        
        db.session.commit()
        
        # Clear session data
        session.pop('registration_data', None)
        
        flash('Registration successful! Your documents are under review. You will be notified within 24-48 hours.', 'success')
        return redirect(url_for('driver.login'))
        
    except Exception as e:
        logger.error(f"Driver registration step 2 error: {e}")
        db.session.rollback()
        flash(f'Registration failed: {str(e)}', 'danger')
        return redirect(url_for('driver.register'))


def _allowed_file(filename):
    """Check if file extension is allowed"""
    allowed_extensions = {'jpg', 'jpeg', 'png', 'pdf'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

@driver_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Driver login"""
    form = DriverLoginForm()
    
    if request.method == 'POST' and form.validate_on_submit():
        driver = Driver.query.filter_by(email=form.email.data).first()
        
        if driver and driver.check_password(form.password.data):
            from flask_login import login_user
            from flask import session
            
            # Set session user type BEFORE login_user to ensure it's available in load_user
            session['user_type'] = 'driver'
            session.modified = True
            
            # Now login the user
            login_user(driver, remember=True)
            
            driver.is_online = True
            db.session.commit()
            
            logger.info(f"Driver {driver.id} ({driver.email}) logged in successfully")
            
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('driver.dashboard'))
        else:
            flash('Invalid email or password', 'danger')
    
    return render_template('driver/login.html', form=form)

@driver_bp.route('/logout', methods=['POST', 'GET'])
@login_required
def logout():
    """Driver logout"""
    # Mark driver as offline
    if isinstance(current_user, Driver):
        current_user.is_online = False
        db.session.commit()
    
    logout_user()
    flash('You have been logged out', 'info')
    return redirect(url_for('driver.login'))

@driver_bp.route('/toggle-status', methods=['POST'])
@login_required
def toggle_status():
    """Toggle driver online/offline status"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        data = request.get_json() or {}
        is_online = data.get('is_online', not current_user.is_online)
        
        # Update driver status
        current_user.is_online = bool(is_online)
        current_user.last_online_update = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'is_online': current_user.is_online,
            'message': f'Driver is now {"online" if current_user.is_online else "offline"}'
        })
        
    except Exception as e:
        logger.error(f"Error toggling driver status: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ===================== DASHBOARD & PROFILE =====================

@driver_bp.route('/dashboard')
@login_required
def dashboard():
    """Driver dashboard"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    
    # Get today's deliveries assigned to this driver
    today = datetime.now().date()
    today_deliveries = Delivery.query.filter_by(driver_id=current_user.id).filter(
        db.func.date(Delivery.created_at) == today
    ).all()
    
    # Get available delivery REQUESTS offered to this driver
    pending_requests = []
    available_deliveries = []
    
    # Build list of available deliveries from requests
    for req in pending_requests:
        delivery = db.session.get(Delivery, req.delivery_id)
        if delivery and delivery.status == 'Pending' and not delivery.driver_id:
            # Attach request info to delivery object for template
            delivery.request_id = req.id
            delivery.expires_at = req.expires_at
            delivery.display_earnings = float(getattr(req, 'driver_estimated_earnings', 0)) if getattr(req, 'driver_estimated_earnings', None) else 0
            available_deliveries.append(delivery)

    completed_today = len([d for d in today_deliveries if d.status == 'Delivered'])
    active_deliveries = len([d for d in today_deliveries if d.status in ['Pending', 'Accepted', 'In Transit']])
    earnings_today = sum([d.actual_driver_earnings or 0 for d in today_deliveries if d.status == 'Delivered'])
    
    # Get historical stats
    all_deliveries = Delivery.query.filter_by(driver_id=current_user.id).all()
    total_completed = len([d for d in all_deliveries if d.status == 'Delivered'])
    total_deliveries_count = len(all_deliveries)
    completion_rate = round((total_completed / total_deliveries_count * 100) if total_deliveries_count > 0 else 0, 1)
    
    # Get rating count from DriverRating table
    from models import DriverRating
    rating_count = DriverRating.query.filter_by(driver_id=current_user.id).count()
    
    stats = {
        'total_deliveries': total_deliveries_count,
        'active_deliveries': active_deliveries,
        'completed_today': completed_today,
        'pending': len([d for d in today_deliveries if d.status in ['Pending', 'Accepted']]),
        'earnings_today': round(earnings_today, 2),
        'total_earnings': round(sum([d.actual_driver_earnings or 0 for d in all_deliveries if d.status == 'Delivered']), 2),
        'is_online': current_user.is_online,
        'available_count': len(available_deliveries),
        'rating': current_user.rating or 0,
        'rating_count': rating_count,
        'completion_rate': completion_rate,
        'avg_rating': current_user.rating or 0
    }
    
    return render_template('driver/dashboard.html', 
                         stats=stats, 
                         deliveries=today_deliveries,
                         available_deliveries=available_deliveries)

@driver_bp.route('/profile')
@login_required
def profile():
    """Driver profile"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    
    return render_template('driver/profile.html', driver=current_user)

@driver_bp.route('/profile/update', methods=['POST'])
@login_required
def update_profile():
    """Update driver profile"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        data = request.get_json()
        
        # Update editable fields
        if 'phone' in data:
            current_user.phone = data.get('phone', '').strip()
        
        if 'vehicle_type' in data:
            current_user.vehicle_type = data.get('vehicle_type', '').strip()
        
        if 'region' in data:
            current_user.region = data.get('region', '').strip()
        
        if 'vehicle_number' in data:
            current_user.vehicle_number = data.get('vehicle_number', '').strip()
        
        current_user.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Profile updated successfully',
            'data': {
                'phone': current_user.phone,
                'vehicle_type': current_user.vehicle_type,
                'region': current_user.region,
                'vehicle_number': current_user.vehicle_number
            }
        })
        
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': 'Failed to update profile',
            'details': str(e)
        }), 400

# ===================== LOCATION TRACKING (Essential Only) =====================

@driver_bp.route('/location/update', methods=['POST'])
@login_required
def update_location():
    """Update driver location"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.json or {}
    logger.debug(f"Location update request data: {data}")
    
    # Get coordinates - try multiple field names
    latitude = data.get('latitude')
    if latitude is None:
        latitude = data.get('lat')
    
    longitude = data.get('longitude')
    if longitude is None:
        longitude = data.get('lng')
    
    delivery_id = data.get('delivery_id')
    
    logger.debug(f"Extracted: latitude={latitude}, longitude={longitude}, delivery_id={delivery_id}, driver_id={current_user.id}")
    
    # Validate coordinates
    if latitude is None or longitude is None:
        logger.warning(f"Missing coordinates: lat={latitude}, lng={longitude}")
        return jsonify({'error': 'Missing coordinates'}), 400
    
    # Try to convert to floats
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (ValueError, TypeError) as e:
        logger.warning(f"Invalid coordinate format: {e}")
        return jsonify({'error': 'Invalid coordinate format'}), 400
    
    try:
        # Update or create location record
        loc = DriverLocation.query.filter_by(driver_id=current_user.id).first()
        if not loc:
            loc = DriverLocation(driver_id=current_user.id)
            logger.debug(f"Creating new location record for driver {current_user.id}")
        
        loc.latitude = latitude
        loc.longitude = longitude
        loc.delivery_id = delivery_id
        loc.updated_at = datetime.utcnow()
        
        db.session.add(loc)
        db.session.commit()
        
        logger.debug(f"Location updated successfully for driver {current_user.id}: {latitude}, {longitude}")
        return jsonify({'message': 'Location updated successfully'})
        
    except Exception as e:
        logger.error(f"Location update database error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@driver_bp.route('/location', methods=['GET'])
@login_required
def get_location():
    """Get driver current location"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        loc = DriverLocation.query.filter_by(driver_id=current_user.id).first()
        if not loc:
            return jsonify({
                'lat': None, 
                'lng': None, 
                'delivery_id': None, 
                'updated_at': None
            })
        
        return jsonify({
            'lat': loc.lat,
            'lng': loc.lng,
            'delivery_id': loc.delivery_id,
            'updated_at': loc.updated_at.isoformat() if loc.updated_at else None
        })
        
    except Exception as e:
        logger.error(f"Get location error: {e}")
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/test', methods=['GET'])
@login_required
def test_endpoint():
    """Test endpoint to verify blueprint is working"""
    return jsonify({
        'success': True,
        'message': 'Driver routes working',
        'user_id': current_user.id,
        'user_type': type(current_user).__name__
    })


@driver_bp.route('/current-location', methods=['GET'])
@login_required
def get_current_location():
    """Get current driver's location"""
    try:
        if not isinstance(current_user, Driver):
            return jsonify({'error': 'Unauthorized'}), 403
        
        print(f"DEBUG: Getting location for driver {current_user.id}")
        
        # Get current driver's location
        location = DriverLocation.query.filter_by(driver_id=current_user.id).first()
        
        print(f"DEBUG: Location found: {location}")
        
        # If no location exists, use driver's region center as fallback
        if not location or not location.latitude or not location.longitude:
            print(f"DEBUG: No location data, using driver's region as fallback: {current_user.region}")
            
            # Region coordinates mapping
            REGION_COORDS = {
                'Savannah': [10.7595, -1.5616],
                'Northern': [10.8021, -1.2871],
                'North East': [10.9055, -0.5515],
                'Upper East': [10.8623, -1.0337],
                'Upper West': [10.3369, -2.3408],
                'Ashanti': [6.6200, -1.6300],
                'Bono': [7.7449, -2.4380],
                'Bono East': [8.0837, -0.6144],
                'Ahafo': [6.8189, -2.2517],
                'Central': [5.1982, -1.2500],
                'Eastern': [6.1256, -0.7597],
                'Greater Accra': [5.6037, -0.1869],
                'Oti': [8.8500, 0.7333],
                'Volta': [6.5000, 0.8333],
                'Western': [5.3667, -2.4333],
                'Western North': [5.5500, -2.8833]
            }
            
            region_coords = REGION_COORDS.get(current_user.region, [5.6037, -0.1869])
            vehicle_type = current_user.vehicle_type if current_user else 'car'
            
            return jsonify({
                'success': True,
                'location': {
                    'driver_id': current_user.id,
                    'lat': float(region_coords[0]),
                    'lng': float(region_coords[1]),
                    'heading': 0,
                    'delivery_id': None,
                    'vehicle_type': vehicle_type.lower(),
                    'timestamp': int(datetime.utcnow().timestamp()),
                    'updated_at': datetime.utcnow().isoformat(),
                    'is_fallback': True
                }
            })
        
        # Get driver info for vehicle type
        vehicle_type = current_user.vehicle_type if current_user else 'car'
        heading_value = getattr(location, 'heading', None)
        heading = float(heading_value) if heading_value else 0
        
        print(f"DEBUG: Returning location for driver {current_user.id}: lat={location.latitude}, lng={location.longitude}, heading={heading}")
        
        return jsonify({
            'success': True,
            'location': {
                'driver_id': location.driver_id,
                'lat': float(location.latitude) if location.latitude else None,
                'lng': float(location.longitude) if location.longitude else None,
                'heading': heading,
                'delivery_id': location.delivery_id,
                'vehicle_type': vehicle_type.lower(),
                'timestamp': int(location.updated_at.timestamp()) if location.updated_at else int(datetime.utcnow().timestamp()),
                'updated_at': location.updated_at.isoformat() if location.updated_at else datetime.utcnow().isoformat()
            }
        })
        
    except Exception as e:
        logger.error(f"Get current location error: {e}")
        print(f"DEBUG: Exception in get_current_location: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/locations/all', methods=['GET'])
@login_required
def get_all_locations():
    """Get all active driver locations (for monitor/dispatcher)"""
    try:
        # Simple query using the model - use correct column names
        all_locations = DriverLocation.query.filter(
            DriverLocation.latitude.isnot(None),
            DriverLocation.longitude.isnot(None)
        ).all()
        
        data = []
        for loc in all_locations:
            # Get driver info for vehicle type
            driver = db.session.get(Driver, loc.driver_id)
            vehicle_type = driver.vehicle_type if driver else 'car'
            
            heading_value = getattr(loc, 'heading', None)
            heading = float(heading_value) if heading_value else 0
            data.append({
                'driver_id': loc.driver_id,
                'lat': float(loc.latitude) if loc.latitude else None,
                'lng': float(loc.longitude) if loc.longitude else None,
                'heading': heading,
                'delivery_id': loc.delivery_id,
                'vehicle_type': vehicle_type.lower(),
                'timestamp': int(loc.updated_at.timestamp()) if loc.updated_at else int(datetime.utcnow().timestamp()),
                'updated_at': loc.updated_at.isoformat() if loc.updated_at else datetime.utcnow().isoformat()
            })
        
        return jsonify({
            'success': True,
            'count': len(data),
            'locations': data
        })
        
    except Exception as e:
        logger.error(f"Get all locations error: {e}")
        # Return empty list instead of error so UI doesn't break
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ===================== BASIC DELIVERY MANAGEMENT =====================

@driver_bp.route('/deliveries', methods=['GET'])
@login_required
def get_deliveries():
    """Get driver's deliveries"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        deliveries = Delivery.query.filter_by(driver_id=current_user.id).order_by(Delivery.created_at.desc()).limit(20).all()
        
        data = []
        for d in deliveries:
            data.append({
                'id': d.id,
                'order_id': d.order_id,
                'status': d.status,
                'pickup_address': d.pickup_address,
                'dropoff_address': d.dropoff_address,
                'created_at': d.created_at.isoformat(),
                'actual_driver_earnings': float(d.actual_driver_earnings) if d.actual_driver_earnings else 0
            })
        
        return jsonify({'deliveries': data}), 200
        
    except Exception as e:
        logger.error(f"Get deliveries error: {e}")
        return jsonify({'error': str(e)}), 500

# Get delivery details for route display
@driver_bp.route('/delivery/<int:delivery_id>')
@login_required
def get_delivery_details(delivery_id):
    """Get delivery details for route display"""
    try:
        delivery = Delivery.query.get(delivery_id)
        
        if not delivery:
            return jsonify({
                'success': False,
                'error': 'Delivery not found'
            }), 404
        
        # Get customer info from order
        customer_name = 'Unknown'
        customer_phone = 'Unknown'
        customer_id = None
        customer_obj = None
        if delivery.order_id:
            order = Order.query.get(delivery.order_id)
            if order:
                customer_name = order.shipping_name or 'Unknown'
                customer_phone = order.shipping_phone or 'Unknown'
                # expose the actual customer id when available
                customer_id = order.customer_id if hasattr(order, 'customer_id') else None
                customer_obj = {
                    'id': customer_id,
                    'full_name': customer_name,
                    'phone': customer_phone
                }
        
        return jsonify({
            'success': True,
            'delivery': {
                'delivery_id': delivery.id,
                'pickup_location': delivery.pickup_location,
                'dropoff_location': delivery.dropoff_location,
                'pickup_lat': float(delivery.pickup_lat) if delivery.pickup_lat else None,
                'pickup_lng': float(delivery.pickup_lng) if delivery.pickup_lng else None,
                'dropoff_lat': float(delivery.dropoff_lat) if delivery.dropoff_lat else None,
                'dropoff_lng': float(delivery.dropoff_lng) if delivery.dropoff_lng else None,
                'distance_km': float(delivery.distance_km) if delivery.distance_km else 0,
                'estimated_duration_minutes': int(delivery.estimated_duration_minutes) if delivery.estimated_duration_minutes else 0,
                'customer_id': customer_id,
                'customer': customer_obj,
                'customer_name': customer_name,
                'customer_phone': customer_phone,
                'driver_earnings': float(delivery.actual_driver_earnings) if delivery.actual_driver_earnings else 0,
                'status': delivery.status
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Error fetching delivery details: {str(e)}")
        return jsonify({
            'success': False,
            'error': 'Failed to fetch delivery details'
        }), 500

@driver_bp.route('/deliveries/<int:delivery_id>/accept', methods=['POST'])
@login_required
def accept_delivery(delivery_id):
    """Accept a delivery"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        delivery = Delivery.query.get_or_404(delivery_id)
        
        if delivery.driver_id and delivery.driver_id != current_user.id:
            return jsonify({'error': 'Delivery already assigned'}), 400
        
        delivery.driver_id = current_user.id
        delivery.status = 'Accepted'
        delivery.accepted_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({'message': 'Delivery accepted successfully'})
        
    except Exception as e:
        logger.error(f"Accept delivery error: {e}")
        return jsonify({'error': str(e)}), 500

@driver_bp.route('/active-delivery', methods=['GET'])
@login_required
def get_active_delivery():
    """Get driver's currently active (Accepted or In Transit) delivery"""
    if not isinstance(current_user, Driver):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        # Look for active deliveries for this driver
        active_delivery = Delivery.query.filter(
            Delivery.driver_id == current_user.id,
            Delivery.status.in_(['Accepted', 'Assigned', 'In Transit'])
        ).first()
        
        if not active_delivery:
            return jsonify({
                'success': False,
                'message': 'No active delivery'
            }), 404
        
        # Get customer info
        customer_data = {
            'id': None,
            'name': 'Unknown',
            'full_name': 'Unknown',
            'phone': None
        }
        
        if active_delivery.order_id:
            order = Order.query.get(active_delivery.order_id)
            if order and order.customer_id:
                customer = Customer.query.get(order.customer_id)
                if customer:
                    customer_data = {
                        'id': customer.id,
                        'name': customer.name,
                        'full_name': customer.name,
                        'phone': customer.phone
                    }
        
        return jsonify({
            'success': True,
            'delivery': {
                'id': active_delivery.id,
                'delivery_id': active_delivery.id,
                'pickup_location': active_delivery.pickup_location,
                'dropoff_location': active_delivery.dropoff_location,
                'pickup_lat': float(active_delivery.pickup_lat) if active_delivery.pickup_lat else None,
                'pickup_lng': float(active_delivery.pickup_lng) if active_delivery.pickup_lng else None,
                'dropoff_lat': float(active_delivery.dropoff_lat) if active_delivery.dropoff_lat else None,
                'dropoff_lng': float(active_delivery.dropoff_lng) if active_delivery.dropoff_lng else None,
                'distance_km': float(active_delivery.distance_km) if active_delivery.distance_km else 0,
                'estimated_duration_minutes': int(active_delivery.estimated_duration_minutes) if active_delivery.estimated_duration_minutes else 0,
                'status': active_delivery.status,
                'customer': customer_data
            }
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching active delivery: {str(e)}")
        return jsonify({
            'success': False,
            'error': 'Failed to fetch active delivery'
        }), 500

@driver_bp.route('/support', methods=['GET'])
@login_required
def support():
    """Support/help page"""
    return render_template('driver/support.html')


# ===================== TRIP LIFECYCLE =====================


@driver_bp.route('/deliveries/<int:delivery_id>/start', methods=['POST'])
@login_required
def start_delivery(delivery_id):
    """Mark a delivery as started (driver left for pickup)"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            return jsonify({'error': 'Delivery not found'}), 404

        if delivery.driver_id != current_user.id:
            return jsonify({'error': 'You are not assigned to this delivery'}), 403

        # Only allow starting if delivery is accepted/assigned
        if delivery.status not in ['Accepted', 'Assigned']:
            return jsonify({'error': f'Cannot start delivery from status {delivery.status}'}), 400

        delivery.status = 'In Transit'
        delivery.started_at = datetime.utcnow()
        db.session.commit()

        logger.info(f"Driver {current_user.id} started delivery {delivery.id}")

        return jsonify({'success': True, 'message': 'Delivery started', 'delivery_id': delivery.id}), 200

    except Exception as e:
        logger.error(f"Error starting delivery {delivery_id}: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/deliveries/<int:delivery_id>/end', methods=['POST'])
@login_required
def end_delivery(delivery_id):
    """Mark a delivery as completed/delivered"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            return jsonify({'error': 'Delivery not found'}), 404

        if delivery.driver_id != current_user.id:
            return jsonify({'error': 'You are not assigned to this delivery'}), 403

        # Only allow ending if delivery is in transit
        if delivery.status != 'In Transit':
            return jsonify({'error': f'Cannot complete delivery from status {delivery.status}'}), 400

        delivery.status = 'Delivered'
        delivery.completed_at = datetime.utcnow()

        # Calculate and persist driver earnings if not set
        try:
            if not delivery.actual_driver_earnings:
                delivery.actual_driver_earnings = delivery.calculate_driver_earnings()
        except Exception:
            # Ignore calculation errors; do not block completion
            pass

        # Update driver stats
        if delivery.driver:
            try:
                delivery.driver.completed_deliveries = (delivery.driver.completed_deliveries or 0) + 1
                # Add earnings to driver's total if available
                if delivery.actual_driver_earnings:
                    try:
                        delivery.driver.total_earnings = (delivery.driver.total_earnings or 0) + delivery.actual_driver_earnings
                    except Exception:
                        # Some DB backends require Decimal; ignore if incompatible
                        pass
            except Exception:
                logger.exception('Failed updating driver stats')

        # Optionally update order status
        if delivery.order_id:
            try:
                order = Order.query.get(delivery.order_id)
                if order:
                    order.status = 'Delivered'
            except Exception:
                logger.exception('Failed updating order status')

        db.session.commit()

        logger.info(f"Driver {current_user.id} completed delivery {delivery.id}")

        return jsonify({'success': True, 'message': 'Delivery completed', 'delivery_id': delivery.id}), 200

    except Exception as e:
        logger.error(f"Error completing delivery {delivery_id}: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500



@driver_bp.route('/api/region', methods=['GET'])
@login_required
def get_driver_region():
    """Get current driver's registered region"""
    if not isinstance(current_user, Driver):
        return {'error': 'Unauthorized'}, 403
    
    region = current_user.region if hasattr(current_user, 'region') else None
    
    # Region coordinates mapping
    REGION_COORDS = {
        'Savannah': [10.7595, -1.5616],
        'Northern': [10.8021, -1.2871],
        'North East': [10.9055, -0.5515],
        'Upper East': [10.8623, -1.0337],
        'Upper West': [10.3369, -2.3408],
        'Ashanti': [6.6200, -1.6300],
        'Bono': [7.7449, -2.4380],
        'Bono East': [8.0837, -0.6144],
        'Ahafo': [6.8189, -2.2517],
        'Central': [5.1982, -1.2500],
        'Eastern': [6.1256, -0.7597],
        'Greater Accra': [5.6037, -0.1869],
        'Oti': [8.8500, 0.7333],
        'Volta': [6.5000, 0.8333],
        'Western': [5.3667, -2.4333],
        'Western North': [5.5500, -2.8833]
    }
    
    coords = REGION_COORDS.get(region, [5.6037, -0.1869])  # Default to Greater Accra if region not found
    
    return {
        'success': True,
        'region': region,
        'coordinates': coords,
        'lat': coords[0],
        'lng': coords[1]
    }, 200


@driver_bp.route('/map', methods=['GET'])
@login_required
def map():
    """Driver map page"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    
    # Get driver's current location
    driver_loc = DriverLocation.query.filter_by(driver_id=current_user.id).first()
    
    # Get active deliveries for the driver
    active_deliveries = Delivery.query.filter_by(
        driver_id=current_user.id
    ).filter(
        Delivery.status.in_(['Assigned', 'In Transit', 'Accepted'])
    ).all()
    
    import os
    routing_service_url = os.environ.get('ROUTING_SERVICE_URL', 'https://routing-service-production-3ce8.up.railway.app')
    
    return render_template('driver/map.html', 
                          driver=current_user,
                          driver_location=driver_loc,
                          active_deliveries=active_deliveries,
                          routing_service_url=routing_service_url)


# ---------------- Driver Messaging & Calls ----------------
@driver_bp.route('/conversations', methods=['GET'])
@login_required
def list_conversations():
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403

    convs = Conversation.query.filter_by(driver_id=current_user.id).order_by(Conversation.last_message_at.desc().nullslast()).all() if hasattr(Conversation, 'last_message_at') else Conversation.query.filter_by(driver_id=current_user.id).all()
    data = []
    for c in convs:
        last_msg = Message.query.filter_by(conversation_id=c.id).order_by(Message.created_at.desc()).first()
        data.append({
            'id': c.id,
            'delivery_id': c.delivery_id,
            'customer_id': c.customer_id,
            'last_message': last_msg.body if last_msg else None,
            'last_message_at': last_msg.created_at.isoformat() if last_msg else (c.last_message_at.isoformat() if c.last_message_at else None)
        })

    return jsonify({'success': True, 'conversations': data}), 200


@driver_bp.route('/conversations/ui', methods=['GET'])
@login_required
def conversations_ui():
    """Render driver conversations UI"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    return render_template('driver/conversations.html')


@driver_bp.route('/conversation/<int:conv_id>/messages', methods=['GET'])
@login_required
def driver_get_messages(conv_id):
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403

    conv = Conversation.query.get_or_404(conv_id)
    if conv.driver_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    messages = Message.query.filter_by(conversation_id=conv.id).order_by(Message.created_at.asc()).all()
    msgs = [
        {'id': m.id, 'sender_type': m.sender_type, 'sender_id': m.sender_id, 'body': m.body, 'created_at': m.created_at.isoformat()}
        for m in messages
    ]
    return jsonify({'success': True, 'messages': msgs}), 200


@driver_bp.route('/conversation/<int:conv_id>/send', methods=['POST'])
@login_required
def driver_send_message(conv_id):
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403

    conv = Conversation.query.get_or_404(conv_id)
    if conv.driver_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    body = data.get('body') or data.get('message')
    if not body:
        return jsonify({'error': 'Empty message'}), 400

    msg = Message(conversation_id=conv.id, sender_type='driver', sender_id=current_user.id, body=body)
    db.session.add(msg)
    conv.last_message_at = datetime.utcnow()
    db.session.commit()

    payload = {
        'conversation_id': conv.id,
        'sender_type': 'driver',
        'sender_id': current_user.id,
        'body': msg.body,
        'created_at': msg.created_at.isoformat(),
        'delivery_id': conv.delivery_id,
        'driver_id': conv.driver_id
    }

    # Notify customer if connected (best-effort)
    try:
        if getattr(current_app, 'socketio', None):
            current_app.socketio.emit('new_message', payload, namespace='/customer', room=f'customer_{conv.customer_id}')
    except Exception:
        logger.exception('Failed to emit new_message to customer')

    return jsonify({'success': True, 'message': payload}), 200


@driver_bp.route('/call/customer/<int:customer_id>', methods=['POST'])
@login_required
def call_customer(customer_id):
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    delivery_id = data.get('delivery_id')

    conv = None
    if delivery_id:
        conv = Conversation.query.filter_by(delivery_id=delivery_id).first()

    call = CallLog(conversation_id=(conv.id if conv else None), caller_type='driver', caller_id=current_user.id, callee_id=customer_id)
    db.session.add(call)
    db.session.commit()

    payload = {'call_id': call.id, 'caller_id': current_user.id, 'caller_name': current_user.full_name, 'delivery_id': delivery_id}
    try:
        if getattr(current_app, 'socketio', None):
            current_app.socketio.emit('incoming_call', payload, namespace='/customer', room=f'customer_{customer_id}')
    except Exception:
        logger.exception('Failed to emit incoming_call to customer')

    return jsonify({'success': True, 'call_id': call.id}), 200


# ===================== WALLET & EARNINGS =====================

@driver_bp.route('/wallet', methods=['GET'])
@login_required
def wallet():
    """Get driver wallet balance and stats"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        wallet = Wallet.query.filter_by(driver_id=current_user.id).first()
        
        if not wallet:
            # Create wallet if doesn't exist
            wallet = Wallet(driver_id=current_user.id, balance=0)
            db.session.add(wallet)
            db.session.commit()
        
        return jsonify({
            'success': True,
            'balance': float(wallet.balance),
            'total_credited': float(wallet.total_credited),
            'total_debitted': float(wallet.total_debitted),
            'created_at': wallet.created_at.isoformat(),
            'updated_at': wallet.updated_at.isoformat()
        }), 200
    
    except Exception as e:
        logger.error(f"Wallet retrieval error: {e}")
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/wallet/history', methods=['GET'])
@login_required
def wallet_history():
    """Get driver wallet transaction history"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        # Get pagination params
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        
        # Get wallet first
        wallet = Wallet.query.filter_by(driver_id=current_user.id).first()
        if not wallet:
            return jsonify({
                'success': True,
                'transactions': [],
                'total': 0,
                'page': page,
                'per_page': per_page
            }), 200
        
        # Query transactions with pagination
        transactions_query = WalletTransaction.query.filter_by(wallet_id=wallet.id).order_by(
            WalletTransaction.created_at.desc()
        )
        
        paginated = transactions_query.paginate(page=page, per_page=per_page)
        
        transactions = [
            {
                'id': txn.id,
                'type': txn.transaction_type,
                'amount': float(txn.amount),
                'description': txn.description,
                'status': txn.status,
                'delivery_id': txn.delivery_id,
                'created_at': txn.created_at.isoformat()
            }
            for txn in paginated.items
        ]
        
        return jsonify({
            'success': True,
            'transactions': transactions,
            'total': paginated.total,
            'pages': paginated.pages,
            'page': page,
            'per_page': per_page
        }), 200
    
    except Exception as e:
        logger.error(f"Wallet history error: {e}")
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/wallet/stats', methods=['GET'])
@login_required
def wallet_stats():
    """Get driver earnings statistics"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        driver = current_user
        
        return jsonify({
            'success': True,
            'total_earnings': float(driver.total_earnings),
            'total_tips_earned': float(driver.total_tips_earned),
            'total_bonuses_earned': float(driver.total_bonuses_earned),
            'total_commissions_paid': float(driver.total_commissions_paid),
            'completed_deliveries': driver.completed_deliveries,
            'cancelled_deliveries': driver.cancelled_deliveries,
            'commission_rate': driver.commission_rate,
            'rating': driver.rating,
            'date_joined': driver.date_joined.isoformat()
        }), 200
    
    except Exception as e:
        logger.error(f"Wallet stats error: {e}")
        return jsonify({'error': str(e)}), 500


# ===================== DELIVERY REQUESTS / OFFERS =====================

@driver_bp.route('/incoming_requests', methods=['GET'])
@login_required
def incoming_requests():
    """Get incoming delivery offers for driver (explicit + available unassigned)"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        requests_data = []
        seen_delivery_ids = set()
        
        # Get ALL available unassigned deliveries (not just explicit offers)
        available_deliveries = Delivery.query.filter(
            Delivery.status == 'Pending',
            Delivery.driver_id == None,
            Delivery.pickup_lat.isnot(None),
            Delivery.pickup_lng.isnot(None)
        ).order_by(Delivery.created_at.desc()).limit(20).all()
        
        logger.info(f"Driver {current_user.id}: Found {len(available_deliveries)} available unassigned deliveries")
        
        for delivery in available_deliveries:
            if delivery.id not in seen_delivery_ids:
                # Check if driver has already cancelled or declined this delivery
                existing_request = DeliveryRequest.query.filter_by(
                    driver_id=current_user.id,
                    delivery_id=delivery.id
                ).first()
                
                # Skip if driver already cancelled or declined this delivery
                if existing_request and existing_request.status in ['Cancelled', 'Declined']:
                    logger.info(f"Driver {current_user.id} already cancelled/declined delivery {delivery.id}, skipping")
                    continue
                
                order = db.session.get(Order, delivery.order_id)
                
                # Estimate earnings (same formula as assignment engine)
                base_fare = float(delivery.base_fare) if delivery.base_fare else 5.0
                per_km_rate = float(delivery.per_km_rate) if delivery.per_km_rate else 1.5
                distance = float(delivery.distance_km) if delivery.distance_km else 2.0
                driver_commission_percent = delivery.driver_commission_percent or 75.0
                estimated_driver_earnings = (base_fare + (distance * per_km_rate)) * (driver_commission_percent / 100.0)
                
                # Calculate expiry (some reasonable time from now)
                now = datetime.utcnow()
                expires_at = now + timedelta(seconds=60)
                
                # Create a temporary DeliveryRequest record for this available delivery
                existing_req = DeliveryRequest.query.filter_by(
                    delivery_id=delivery.id,
                    driver_id=current_user.id,
                    status='Pending'
                ).first()
                
                if not existing_req:
                    temp_request = DeliveryRequest(
                        delivery_id=delivery.id,
                        driver_id=current_user.id,
                        status='Pending',
                        sent_at=now,
                        expires_at=expires_at
                    )
                    db.session.add(temp_request)
                    db.session.flush()
                    request_id = temp_request.id
                else:
                    request_id = existing_req.id
                    existing_req.sent_at = now
                    existing_req.expires_at = expires_at
                
                request_info = {
                    'request_id': request_id,
                    'delivery_id': delivery.id,
                    'status': 'Pending',
                    'pickup_location': delivery.pickup_location,
                    'dropoff_location': delivery.dropoff_location,
                    'pickup_lat': delivery.pickup_lat,
                    'pickup_lng': delivery.pickup_lng,
                    'dropoff_lat': delivery.dropoff_lat if delivery.dropoff_lat else (order.latitude if order else None),
                    'dropoff_lng': delivery.dropoff_lng if delivery.dropoff_lng else (order.longitude if order else None),
                    'distance_km': delivery.distance_km,
                    'estimated_duration_minutes': delivery.estimated_duration_minutes,
                    'estimated_fare': base_fare + (distance * per_km_rate),
                    'driver_estimated_earnings': estimated_driver_earnings,
                    'sent_at': now.isoformat(),
                    'expires_at': expires_at.isoformat(),
                    'seconds_until_expiry': 60,
                    'is_expired': False,
                    'customer_name': order.shipping_name if order else 'Unknown',
                    'customer_phone': order.shipping_phone if order else None,
                    'offer_type': 'available'
                }
                requests_data.append(request_info)
                seen_delivery_ids.add(delivery.id)
        
        # Commit any temporary DeliveryRequest records
        if len(requests_data) > 0:
            db.session.commit()
        
        logger.info(f"Driver {current_user.id}: Returning {len(requests_data)} total requests")
        
        return jsonify({
            'success': True,
            'requests': requests_data,
            'count': len(requests_data)
        }), 200
    
    except Exception as e:
        logger.error(f"Error fetching incoming requests: {e}")
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/requests/<int:request_id>/accept', methods=['POST'])
@login_required
def accept_delivery_request(request_id):
    """Driver accepts a delivery offer"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        from delivery_assignment_engine import driver_accept_delivery_offer

        # Log attempt details to help debug accept failures
        logger.info(f"Driver {current_user.id} attempting to accept request {request_id}")
        pre_req = db.session.get(DeliveryRequest, request_id)
        if pre_req:
            logger.info(f"DeliveryRequest {request_id} pre-check: driver_id={pre_req.driver_id}, status={pre_req.status}, delivery_id={pre_req.delivery_id}")
            pre_delivery = db.session.get(Delivery, pre_req.delivery_id)
            if pre_delivery:
                logger.info(f"Associated Delivery {pre_delivery.id} pre-check: status={pre_delivery.status}, driver_id={pre_delivery.driver_id}")
            else:
                logger.info(f"Associated Delivery {pre_req.delivery_id} not found during pre-check for request {request_id}")
        else:
            logger.info(f"DeliveryRequest {request_id} not found during pre-check")

        success = driver_accept_delivery_offer(current_user.id, request_id)

        if success:
            delivery_request = db.session.get(DeliveryRequest, request_id)
            delivery = db.session.get(Delivery, delivery_request.delivery_id)

            return jsonify({
                'success': True,
                'message': 'Delivery accepted!',
                'delivery_id': delivery.id
            }), 200

        # If engine returned False, provide more granular diagnostics for the client/logs
        delivery_request = db.session.get(DeliveryRequest, request_id)
        if not delivery_request:
            logger.info(f"Accept failed: DeliveryRequest {request_id} not found for driver {current_user.id}")
            return jsonify({'success': False, 'error': 'Delivery request not found'}), 404

        # Wrong driver
        if delivery_request.driver_id != current_user.id:
            logger.info(f"Accept failed: Driver {current_user.id} attempted to accept request {request_id} owned by driver {delivery_request.driver_id}")
            return jsonify({'success': False, 'error': 'This request was offered to another driver'}), 403

        # Non-pending status
        if delivery_request.status != 'Pending':
            logger.info(f"Accept failed: DeliveryRequest {request_id} status is {delivery_request.status}")
            return jsonify({'success': False, 'error': f'Request is not pending (status={delivery_request.status})'}), 400

        # Delivery missing
        delivery = db.session.get(Delivery, delivery_request.delivery_id)
        if not delivery:
            logger.info(f"Accept failed: Delivery {delivery_request.delivery_id} not found for request {request_id}")
            return jsonify({'success': False, 'error': 'Associated delivery not found'}), 404

        # Generic fallback
        logger.info(f"Accept failed: Unknown reason for request {request_id} by driver {current_user.id}")
        return jsonify({'success': False, 'error': 'Failed to accept delivery request'}), 400
    
    except Exception as e:
        logger.error(f"Error accepting delivery request: {e}")
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/requests/<int:request_id>/delete', methods=['POST'])
@login_required
def delete_delivery_request(request_id):
    """Driver removes a delivery offer from their view"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        # Find the delivery request for this driver
        delivery_request = DeliveryRequest.query.filter_by(
            driver_id=current_user.id,
            id=request_id
        ).first()
        
        if not delivery_request:
            return jsonify({'error': 'Request not found'}), 404
        
        # Update status to indicate this driver has removed it
        # Use 'Cancelled' status to distinguish from regular 'Declined'
        delivery_request.status = 'Cancelled'
        delivery_request.declined_at = datetime.utcnow()
        delivery_request.decline_reason = 'Removed by driver'
        
        db.session.commit()
        
        logger.info(f"Driver {current_user.id} removed delivery request {request_id} from their view")
        
        return jsonify({
            'success': True,
            'message': 'Delivery request removed from your view'
        }), 200
        
    except Exception as e:
        logger.error(f"Error removing delivery request: {e}")
        return jsonify({'error': str(e)}), 500


@driver_bp.route('/requests/<int:request_id>/decline', methods=['POST'])
@login_required
def decline_delivery_request(request_id):
    """Driver declines a delivery offer"""
    if not isinstance(current_user, Driver):
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        data = request.get_json() or {}
        reason = data.get('reason', 'Too far')
        
        from delivery_assignment_engine import driver_decline_delivery_offer
        
        success = driver_decline_delivery_offer(current_user.id, request_id, reason)
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Delivery declined'
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to decline delivery request'
            }), 400
    
    except Exception as e:
        logger.error(f"Error declining delivery request: {e}")
        return jsonify({'error': str(e)}), 500


# ===================== ONBOARDING & DOCUMENTS =====================

@driver_bp.route('/onboarding')
@login_required
def onboarding_status():
    """Display driver onboarding and verification status"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    
    from models import DriverOnboarding, Document
    
    onboarding = DriverOnboarding.query.filter_by(driver_id=current_user.id).first()
    
    if not onboarding:
        # Create onboarding record if it doesn't exist
        onboarding = DriverOnboarding(driver_id=current_user.id)
        db.session.add(onboarding)
        db.session.commit()
    
    documents = Document.query.filter_by(onboarding_id=onboarding.id).all()
    
    # Calculate completion percentage (5 required documents)
    required_docs = ['national_id', 'driver_license', 'vehicle_insurance', 'vehicle_inspection', 'ghana_card']
    submitted_count = len([d for d in documents if d.verification_status in ['Verified', 'Pending']])
    completion_percentage = (submitted_count / len(required_docs)) * 100 if required_docs else 0
    
    return render_template(
        'driver/onboarding_status.html',
        onboarding=onboarding,
        documents=documents,
        completion_percentage=int(completion_percentage),
        required_docs=required_docs
    )


@driver_bp.route('/documents/upload', methods=['POST'])
@login_required
def upload_document():
    """Upload a driver document"""
    if not isinstance(current_user, Driver):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    from models import DriverOnboarding, Document
    import os
    from werkzeug.utils import secure_filename
    
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        
        file = request.files['file']
        document_type = request.form.get('document_type')
        document_number = request.form.get('document_number')
        
        if not document_type:
            return jsonify({'success': False, 'error': 'Document type required'}), 400
        
        # Get or create onboarding record
        onboarding = DriverOnboarding.query.filter_by(driver_id=current_user.id).first()
        if not onboarding:
            onboarding = DriverOnboarding(driver_id=current_user.id)
            db.session.add(onboarding)
            db.session.flush()
        
        # Save file
        if file and file.filename:
            filename = secure_filename(f"doc_{onboarding.id}_{document_type}_{datetime.utcnow().timestamp()}")
            upload_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'drivers', 'documents')
            os.makedirs(upload_dir, exist_ok=True)
            
            filepath = os.path.join(upload_dir, filename)
            file.save(filepath)
            
            # Store relative path for database
            file_path = os.path.join('drivers', 'documents', filename)
            
            # Create document record
            doc = Document(
                onboarding_id=onboarding.id,
                document_type=document_type,
                file_name=file.filename,
                file_path=file_path,
                file_size=os.path.getsize(filepath),
                mime_type=file.content_type,
                document_number=document_number
            )
            
            db.session.add(doc)
            db.session.commit()
            
            logger.info(f"Driver {current_user.id} uploaded {document_type} document")
            
            return jsonify({
                'success': True,
                'message': f'{document_type} uploaded successfully',
                'document_id': doc.id
            }), 200
        else:
            return jsonify({'success': False, 'error': 'Invalid file'}), 400
    
    except Exception as e:
        logger.error(f"Error uploading document: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@driver_bp.route('/payments')
@login_required
def payment_statements():
    """Display weekly payment statements"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    
    from models import PaymentStatement
    
    # Get all payment statements for this driver, ordered by most recent first
    statements = PaymentStatement.query.filter_by(driver_id=current_user.id).order_by(
        PaymentStatement.period_start.desc()
    ).all()
    
    # Calculate YTD totals
    ytd_earnings = sum([s.net_payment for s in statements if s.payment_status == 'Paid']) if statements else 0
    ytd_deliveries = sum([s.deliveries_count for s in statements]) if statements else 0
    avg_rating = sum([s.average_rating for s in statements if s.average_rating]) / len([s for s in statements if s.average_rating]) if any(s.average_rating for s in statements) else 0
    
    return render_template(
        'driver/payment_statements.html',
        statements=statements,
        ytd_earnings=ytd_earnings,
        ytd_deliveries=ytd_deliveries,
        avg_rating=avg_rating
    )




# ===================== EARNINGS ANALYTICS =====================

@driver_bp.route('/analytics')
@login_required
def earnings_analytics():
    """Display earnings analytics dashboard"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    
    from models import DriverEarningsMetric, PaymentStatement
    from datetime import datetime, timedelta
    
    # Get last 30 days of earnings data
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    metrics = DriverEarningsMetric.query.filter(
        DriverEarningsMetric.driver_id == current_user.id,
        DriverEarningsMetric.date >= thirty_days_ago
    ).order_by(DriverEarningsMetric.date).all()
    
    # Get payment statements
    statements = PaymentStatement.query.filter_by(driver_id=current_user.id).order_by(
        PaymentStatement.period_start.desc()
    ).limit(12).all()
    
    # Calculate peak hours from metrics
    peak_hours = {}
    for metric in metrics:
        if metric.hour is not None and metric.is_peak_hour:
            hour_key = f"{metric.hour:02d}:00"
            peak_hours[hour_key] = peak_hours.get(hour_key, 0) + 1
    
    # Calculate best earning locations
    location_earnings = {}
    for metric in metrics:
        if metric.location_name:
            if metric.location_name not in location_earnings:
                location_earnings[metric.location_name] = {'earnings': 0, 'deliveries': 0, 'rating': 0}
            location_earnings[metric.location_name]['earnings'] += float(metric.earnings or 0)
            location_earnings[metric.location_name]['deliveries'] += metric.deliveries_count
            if metric.average_rating:
                location_earnings[metric.location_name]['rating'] = float(metric.average_rating)
    
    # Sort by earnings
    top_locations = sorted(location_earnings.items(), key=lambda x: x[1]['earnings'], reverse=True)[:5]
    
    # Calculate correlation between acceptance rate and earnings
    correlation_data = []
    for metric in metrics:
        if metric.acceptance_rate and metric.earnings:
            correlation_data.append({
                'acceptance': float(metric.acceptance_rate),
                'earnings': float(metric.earnings)
            })
    
    return render_template(
        'driver/earnings_analytics.html',
        metrics=metrics,
        statements=statements,
        peak_hours=peak_hours,
        top_locations=top_locations,
        correlation_data=correlation_data
    )


# ===================== RATING & PERFORMANCE =====================

@driver_bp.route('/ratings')
@login_required
def driver_ratings():
    """Display driver ratings and performance"""
    if not isinstance(current_user, Driver):
        return redirect(url_for('driver.login'))
    
    from models import RatingFeedback, PaymentStatement
    
    # Get all ratings for this driver
    ratings = RatingFeedback.query.filter_by(driver_id=current_user.id).order_by(
        RatingFeedback.created_at.desc()
    ).all()
    
    # Calculate stats
    total_ratings = len(ratings)
    avg_rating = sum([r.rating for r in ratings]) / total_ratings if total_ratings > 0 else 0
    
    # Rating breakdown by category
    category_stats = {}
    for rating in ratings:
        if rating.category not in category_stats:
            category_stats[rating.category] = {'count': 0, 'total': 0}
        category_stats[rating.category]['count'] += 1
        category_stats[rating.category]['total'] += rating.rating
    
    for category in category_stats:
        category_stats[category]['avg'] = category_stats[category]['total'] / category_stats[category]['count']
    
    # Rating distribution (1-5 stars)
    rating_distribution = {
        1: len([r for r in ratings if r.rating == 1]),
        2: len([r for r in ratings if r.rating == 2]),
        3: len([r for r in ratings if r.rating == 3]),
        4: len([r for r in ratings if r.rating == 4]),
        5: len([r for r in ratings if r.rating == 5])
    }
    
    # Get latest payment statement for commission info
    latest_statement = PaymentStatement.query.filter_by(driver_id=current_user.id).order_by(
        PaymentStatement.period_start.desc()
    ).first()
    
    # Determine rating impact on commission
    commission_impact = {
        '5.0': '0% commission',
        '4.5-4.9': '5% commission',
        '4.0-4.4': '10% commission',
        'below-4.0': '15% commission (needs improvement)'
    }
    
    return render_template(
        'driver/driver_ratings.html',
        ratings=ratings,
        total_ratings=total_ratings,
        avg_rating=avg_rating,
        category_stats=category_stats,
        rating_distribution=rating_distribution,
        latest_statement=latest_statement,
        commission_impact=commission_impact
    )



