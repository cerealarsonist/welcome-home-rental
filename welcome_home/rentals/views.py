import json
# import google.generativeai as genai  # Lazy loaded below
# from google.generativeai.types import GenerateContentConfig as ContentConfig

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import BookingForm, CommentForm, PropertyForm
from .models import Booking, Comment, Property, PropertyImage, SavedProperty
from chatapp.models import ChatRoom


def home(request):
    properties = Property.objects.filter(is_available=True).order_by('-created_at')[:8]
    recommendations = Property.objects.filter(
        is_available=True,
        recommendation_score__isnull=False,
        recommendation_score__gt=0
    ).order_by('-recommendation_score')[:4]

    return render(request, 'home.html', {
        'recommendations': recommendations,
        'properties': properties,
    })


def property_list(request):
    query = request.GET.get('q', '')
    property_type_filter = request.GET.get('property_type', '')
    min_price_str = request.GET.get('min_price', '')
    max_price_str = request.GET.get('max_price', '')
    amenities_filter = request.GET.get('amenities', '')
    available_only = request.GET.get('available_only') == 'on'

    properties = Property.objects.order_by('-created_at').distinct()

    # Text search
    if query:
        properties = properties.filter(
            Q(title__icontains=query) |
            Q(location__icontains=query) |
            Q(description__icontains=query) |
            Q(amenities__icontains=query) |
            Q(tags__name__icontains=query)
        ).distinct()

    # Property type filter
    if property_type_filter:
        properties = properties.filter(property_type=property_type_filter)

    # Price range
    if min_price_str:
        try:
            min_price = float(min_price_str)
            properties = properties.filter(price__gte=min_price)
        except ValueError:
            pass

    if max_price_str:
        try:
            max_price = float(max_price_str)
            properties = properties.filter(price__lte=max_price)
        except ValueError:
            pass

    # Amenities filter
    if amenities_filter:
        properties = properties.filter(amenities__icontains=amenities_filter)

    # Availability
    if available_only:
        properties = properties.filter(is_available=True)

    ai_guide = None
    recommended_properties = []

    # AI guide only (no examples)
    if query and getattr(settings, 'GEMINI_API_KEY', None):
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-2.5-flash')
            prompt = f"""You are a real estate assistant for students in Manila. User search: "{query}"

Return a short 1-2 sentence guide about renting properties matching "{query}" in Manila. No JSON needed."""
            response = model.generate_content(prompt)
            ai_guide = response.text.strip()
        except Exception as e:
            print(f"AI Guide Error: {e}")

    # Recommend top real properties (all available, sorted by score)
    all_properties = Property.objects.filter(is_available=True).order_by('-recommendation_score')[:4]
    recommended_properties = list(all_properties)

    context = {
        'properties': properties,
        'query': query,
        'property_type_filter': property_type_filter,
        'min_price': min_price_str,
        'max_price': max_price_str,
        'amenities_filter': amenities_filter,
        'available_only': available_only,
        'ai_guide': ai_guide,
        'recommended_properties': recommended_properties,
    }
    return render(request, 'rentals/property_list.html', context)


def about(request):
    return render(request, 'about.html')


def contact(request):
    return render(request, 'contact.html')


def property_detail(request, pk):
    property_obj = get_object_or_404(Property, pk=pk)
    comments = property_obj.comments.all().order_by('-created_at')
    booking_form = BookingForm()
    comment_form = CommentForm()

    is_saved = False
    if request.user.is_authenticated and getattr(request.user, 'role', None) == 'renter':
        is_saved = SavedProperty.objects.filter(
            renter=request.user,
            property=property_obj
        ).exists()

    candidate_properties = Property.objects.filter(
        is_available=True
    ).exclude(pk=property_obj.pk).distinct()

    property_tags = set(property_obj.tags.values_list('name', flat=True))
    recommended_properties = []

    for item in candidate_properties:
        score = 0
        reasons = []

        if item.property_type == property_obj.property_type:
            score += 30
            reasons.append("Same property type")

        price_gap = abs(float(item.price) - float(property_obj.price))
        if price_gap <= 1000:
            score += 25
            reasons.append("Very close in price")
        elif price_gap <= 3000:
            score += 15
            reasons.append("Similar rental price")
        elif price_gap <= 5000:
            score += 8
            reasons.append("Reasonably close in budget")

        item_tags = set(item.tags.values_list('name', flat=True))
        common_tags = property_tags.intersection(item_tags)
        if common_tags:
            score += min(len(common_tags) * 10, 20)
            reasons.append("Similar tags: " + ", ".join(list(common_tags)[:3]))

        location_a = (property_obj.location or '').lower()
        location_b = (item.location or '').lower()
        if location_a and location_b:
            for keyword in ['sampaloc', 'ust', 'feu', 'ceu', 'p. campa', 'dapitan', 'espana']:
                if keyword in location_a and keyword in location_b:
                    score += 15
                    reasons.append("Similar area")
                    break

        amenities_a = (property_obj.amenities or '').lower()
        amenities_b = (item.amenities or '').lower()
        common_amenity_keywords = []
        for keyword in ['wifi', 'aircon', 'cabinet', 'bed', 'study', 'kitchen', 'security']:
            if keyword in amenities_a and keyword in amenities_b:
                common_amenity_keywords.append(keyword)

        if common_amenity_keywords:
            score += min(len(common_amenity_keywords) * 3, 12)
            reasons.append("Shared amenities: " + ", ".join(common_amenity_keywords[:3]))

        item.recommendation_score_temp = score

        if score >= 65:
            item.recommendation_level = "High Match"
        elif score >= 40:
            item.recommendation_level = "Medium Match"
        else:
            item.recommendation_level = "Basic Match"

        item.recommendation_reason = ", ".join(reasons) if reasons else "Similar listing preference"
        recommended_properties.append(item)

    recommended_properties = sorted(
        recommended_properties,
        key=lambda x: getattr(x, 'recommendation_score_temp', 0),
        reverse=True
    )[:4]

    if request.method == 'POST':
        if 'booking_submit' in request.POST:
            if not request.user.is_authenticated:
                messages.error(request, "Please log in first before booking.")
                return redirect('login')

            if getattr(request.user, 'role', None) != 'renter':
                messages.error(request, "Only renters can book properties.")
                return redirect('property_detail', pk=pk)

            booking_form = BookingForm(request.POST)
            if booking_form.is_valid():
                booking = booking_form.save(commit=False)
                booking.renter = request.user
                booking.property = property_obj
                booking.save()
                messages.success(request, "Booking submitted successfully.")
                return redirect('renter_bookings')

        elif 'comment_submit' in request.POST:
            if not request.user.is_authenticated:
                messages.error(request, "Please log in first before commenting.")
                return redirect('login')

            comment_form = CommentForm(request.POST)
            if comment_form.is_valid():
                comment = comment_form.save(commit=False)
                comment.user = request.user
                comment.property = property_obj
                comment.save()
                messages.success(request, "Comment posted successfully.")
                return redirect('property_detail', pk=pk)

    return render(request, 'rentals/property_detail.html', {
        'property': property_obj,
        'comments': comments,
        'booking_form': booking_form,
        'comment_form': comment_form,
        'is_saved': is_saved,
        'similar_properties': recommended_properties,
    })


@login_required
def property_create(request):
    if getattr(request.user, 'role', None) not in ['landlord', 'admin']:
        messages.error(request, "Only landlords or admins can add properties.")
        return redirect('dashboard')

    if request.method == 'POST':
        form = PropertyForm(request.POST, request.FILES)
        if form.is_valid():
            prop = form.save(commit=False)
            prop.landlord = request.user
            prop.save()
            form.save_m2m()

            gallery_files = request.FILES.getlist('gallery_images')
            for image_file in gallery_files:
                PropertyImage.objects.create(property=prop, image=image_file)

            messages.success(request, "Property added successfully.")
            return redirect('property_detail', pk=prop.pk)
    else:
        form = PropertyForm()

    return render(request, 'rentals/property_form.html', {
        'form': form,
        'edit_mode': False,
    })


@login_required
def property_edit(request, pk):
    property_obj = get_object_or_404(Property, pk=pk, landlord=request.user)

    if request.method == 'POST':
        form = PropertyForm(request.POST, request.FILES, instance=property_obj)
        if form.is_valid():
            prop = form.save()
            gallery_files = request.FILES.getlist('gallery_images')
            for image_file in gallery_files:
                PropertyImage.objects.create(property=prop, image=image_file)

            messages.success(request, "Property updated successfully.")
            return redirect('property_detail', pk=pk)
    else:
        form = PropertyForm(instance=property_obj)

    return render(request, 'rentals/property_form.html', {
        'form': form,
        'edit_mode': True,
    })


@login_required
def booking_page(request, pk):
    property_obj = get_object_or_404(Property, pk=pk)

    if getattr(request.user, 'role', None) != 'renter':
        return redirect('property_detail', pk=pk)

    if request.method == 'POST':
        form = BookingForm(request.POST)
        if form.is_valid():
            booking = form.save(commit=False)
            booking.renter = request.user
            booking.property = property_obj
            booking.save()
            messages.success(request, "Booking submitted successfully.")
            return redirect('renter_bookings')
    else:
        form = BookingForm()

    return render(request, 'rentals/booking_form.html', {
        'form': form,
        'property': property_obj
    })


@login_required
def save_property(request, pk):
    if getattr(request.user, 'role', None) != 'renter':
        return redirect('property_detail', pk=pk)

    property_obj = get_object_or_404(Property, pk=pk)
    SavedProperty.objects.get_or_create(renter=request.user, property=property_obj)
    messages.success(request, "Property saved successfully.")
    return redirect('property_detail', pk=pk)


@login_required
def unsave_property(request, pk):
    if getattr(request.user, 'role', None) != 'renter':
        return redirect('property_detail', pk=pk)

    property_obj = get_object_or_404(Property, pk=pk)
    SavedProperty.objects.filter(renter=request.user, property=property_obj).delete()
    messages.success(request, "Property removed from saved list.")
    return redirect('property_detail', pk=pk)


@login_required
def message_landlord(request, pk):
    property_obj = get_object_or_404(Property, pk=pk)

    if not request.user.is_authenticated:
        return redirect('login')

    if request.user == property_obj.landlord:
        messages.info(request, "This is your own property.")
        return redirect('property_detail', pk=pk)

    room_name = f"property-{property_obj.pk}-{min(request.user.id, property_obj.landlord.id)}-{max(request.user.id, property_obj.landlord.id)}"
    room, created = ChatRoom.objects.get_or_create(
        property=property_obj,
        renter=request.user if getattr(request.user, 'role', None) == 'renter' else property_obj.landlord,
        landlord=property_obj.landlord,
        defaults={'name': room_name}
    )

    room.participants.add(request.user)
    room.participants.add(property_obj.landlord)

    return redirect('chat_room', room.id)