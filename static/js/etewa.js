/* ==========================================================
   ETEWA — Single-file build
   Modules: auth, trips, requests, messaging, categories, UI
   ========================================================== */

/* ---------------- 1. AUTH ---------------- */
(function () {
  'use strict';
  var currentUser = null;
  var token = null;
  var sessionCheck = null;

  function account(data) {
    return data ? {
      id: data.id, name: data.full_name, email: data.email, city: data.city || '',
      bio: data.bio || '', phone: data.phone || '', joinedAt: data.created_at, averageRating: null
    } : null;
  }

  async function request(path, method, body) {
    var headers = { Accept: 'application/json' };
    if (method !== 'GET') {
      if (!token) { await restore(); }
      headers['X-CSRF-Token'] = token;
      if (body !== undefined) { headers['Content-Type'] = 'application/json'; }
    }
    var response;
    try {
      response = await fetch('/api/' + path, {
        method: method, headers: headers, credentials: 'same-origin',
        body: body === undefined ? undefined : JSON.stringify(body)
      });
    } catch (_) { throw new Error('Could not reach Etewa. Check your connection and try again.'); }
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      if (response.status === 401 && path === 'profile') { currentUser = null; token = null; }
      throw new Error(data.error || 'Etewa could not complete that request. Try again later.');
    }
    if (data.csrf_token) { token = data.csrf_token; }
    return data;
  }

  async function applyAccount(path, method, body) {
    var data = await request(path, method, body);
    currentUser = account(data.user);
    return currentUser;
  }

  function restore() {
    if (!sessionCheck) {
      sessionCheck = applyAccount('me', 'GET').finally(function () { sessionCheck = null; });
    }
    return sessionCheck;
  }

  window.EtewaAuth = {
    request: request,
    getCurrentUser: function () { return currentUser; },
    restore: restore,
    isServerAccountId: function (id) { return typeof id === 'string' && /^account:[1-9]\d*$/.test(id); },
    register: function (name, email, password, city) {
      return applyAccount('register', 'POST', {full_name:name, email:email, password:password, city:city});
    },
    login: function (email, password) {
      return applyAccount('login', 'POST', {email:email, password:password});
    },
    logout: function () { return applyAccount('logout', 'POST'); },
    getProfile: async function () { return account((await request('profile', 'GET')).user); },
    updateProfile: function (updates) { return applyAccount('profile', 'PATCH', updates); }
  };
})();

/* ---------------- 2. TRIPS ---------------- */
(function () {
  'use strict';
  var legacyTrips = [];
  try {
    var stored = JSON.parse(localStorage.getItem('etewa_published_trips') || '[]');
    if (Array.isArray(stored)) {
      legacyTrips = stored.filter(function (trip) {
        return trip && typeof trip.id === 'string' && trip.id.startsWith('trip_') &&
          typeof trip.date === 'string' && typeof trip.traveler?.name === 'string';
      });
    }
  } catch (_) { /* Leave unreadable legacy storage untouched. */ }

  function displayTrip(trip) {
    return {
      id: 'db-trip:' + trip.id, databaseId: trip.id, isShared: true, status: trip.status,
      ownerId: trip.traveler.id,
      from: EtewaData.country(trip.departure_country)?.name || trip.departure_country,
      to: EtewaData.country(trip.destination_country)?.name || trip.destination_country,
      date: trip.travel_date, weight: trip.capacity_grams / 1000, availableGrams: trip.available_capacity_grams,
      pricing: trip.pricing_method, price: trip.price_minor === null ? null : trip.price_minor / 100,
      notes: trip.notes,
      traveler: {name:trip.traveler.full_name, city:trip.traveler.city}
    };
  }

  function api(path, method, data) {
    return window.EtewaAuth.request(path, method, data);
  }

  window.EtewaTrips = {
    getLegacyTrips: function () { return legacyTrips.slice(); },
    findLegacyTrip: function (id) { return legacyTrips.find(function (trip) { return trip.id === id; }); },
    loadPage: async function (path, params) {
      var result = await api(path + '?' + new URLSearchParams(params), 'GET');
      return {items:result.trips.map(displayTrip), page:result.page, total:result.total, hasNext:result.has_next};
    },
    publishTrip: function (details) { return api('trips', 'POST', details); },
    updateTrip: function (id, details) { return api('trips/' + id, 'PATCH', details); },
    archiveTrip: function (id) { return api('trips/' + id, 'DELETE'); }
  };
})();

/* ---------------- 3. REQUESTS ---------------- */
(function () {
  'use strict';
  var REQUESTS_STORAGE_KEY = 'etewa_delivery_requests';
  var allRequests = [];
  var currentUser = null;
  function loadRequests() { var raw = localStorage.getItem(REQUESTS_STORAGE_KEY); allRequests = raw ? JSON.parse(raw) : []; }
  function saveRequests() { localStorage.setItem(REQUESTS_STORAGE_KEY, JSON.stringify(allRequests)); }
  var STATUS_LABELS = { requested:'Requested', accepted:'Accepted', in_transit:'In Transit', delivered:'Delivered', cancelled:'Cancelled' };

  window.EtewaRequests = {
    setCurrentUser: function (u) { currentUser = u; },
    countCompleted: function (travelerId) {
      return allRequests.filter(function (request) {
        return request.travelerId === travelerId && request.status === 'delivered';
      }).length;
    },
    getStatusLabel: function (s) { return STATUS_LABELS[s] || s; },
    remainingCapacity: function (trip) {
      var committed = allRequests.filter(function (req) {
        return req.tripId === String(trip.id) && ['accepted','in_transit','delivered'].includes(req.status);
      }).reduce(function (total, req) { return total + (EtewaData.positive(req.weight) ? Number(req.weight) : 0); }, 0);
      return Math.max(0, Math.round((Number(trip.weight) - committed) * 1e9) / 1e9);
    },
    createRequest: function (trip, details) {
      if (!currentUser || !window.EtewaTrips.findLegacyTrip(trip.id) || !window.EtewaAuth.isServerAccountId(trip.ownerId) || trip.ownerId === currentUser.id || trip.date < EtewaData.today() ||
          !EtewaData.positive(details.weight) || details.weight > this.remainingCapacity(trip)) { return null; }
      var existing = allRequests.find(function (r) {
        return r.senderId === currentUser.id && r.tripId === String(trip.id) && r.status !== 'cancelled' && r.status !== 'delivered';
      });
      if (existing) { return null; }
      var newReq = {
        id: 'request_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
        senderId: currentUser.id, senderName: currentUser.name,
        senderProfile: {name:currentUser.name, city:currentUser.city, bio:currentUser.bio},
        travelerId: trip.ownerId || null, travelerName: trip.traveler.name,
        tripId: String(trip.id), tripFrom: trip.from, tripTo: trip.to, tripDate: trip.date,
        category: details.category, otherExplanation: details.otherExplanation || '',
        weight: details.weight, description: details.description,
        pricing: trip.pricing || 'agreement', tripPrice: trip.price || null, proposedPrice: details.proposedPrice,
        status: 'requested', createdAt: new Date().toISOString(),
        statusHistory: [{ status:'requested', at: new Date().toISOString(), byName: currentUser.name }]
      };
      allRequests.push(newReq); saveRequests(); return newReq;
    },
    getRequestsSentByCurrentUser: function () { return currentUser ? allRequests.filter(function (r) { return r.senderId === currentUser.id; }) : []; },
    getRequestsForTripsOwnedByCurrentUser: function () { return currentUser ? allRequests.filter(function (r) { return r.travelerId === currentUser.id; }) : []; },
    updateRequestStatus: function (requestId, newStatus) {
      if (!currentUser) { return false; }
      var request = allRequests.find(function (r) { return r.id === requestId; });
      if (!request) { return false; }
      var allowed = false;
      if (request.travelerId === currentUser.id) {
        if (request.status === 'requested' && (newStatus === 'accepted' || newStatus === 'cancelled')) { allowed = true; }
        if (request.status === 'accepted' && newStatus === 'in_transit') { allowed = true; }
        if (request.status === 'in_transit' && newStatus === 'delivered') { allowed = true; }
      }
      if (request.senderId === currentUser.id && newStatus === 'cancelled' && (request.status === 'requested' || request.status === 'accepted')) { allowed = true; }
      if (!allowed) { return false; }
      if (newStatus === 'accepted') {
        var trip = window.EtewaTrips.findLegacyTrip(request.tripId);
        if (!trip || trip.date < EtewaData.today() || !EtewaData.positive(request.weight) || Number(request.weight) > this.remainingCapacity(trip)) { return false; }
      }
      request.status = newStatus;
      request.statusHistory.push({ status: newStatus, at: new Date().toISOString(), byName: currentUser.name });
      saveRequests(); return true;
    }
  };
  loadRequests();
})();

/* ---------------- 4. MESSAGING (with unread tracking) ---------------- */
(function () {
  'use strict';
  var CONVERSATIONS_STORAGE_KEY = 'etewa_conversations';
  var READS_STORAGE_KEY = 'etewa_last_read';
  var allConversations = [];
  var lastReadMap = {};   // { userId: { conversationId: iso } }
  var currentUser = null;

  function loadConversations() { var raw = localStorage.getItem(CONVERSATIONS_STORAGE_KEY); allConversations = raw ? JSON.parse(raw) : []; }
  function saveConversations() { localStorage.setItem(CONVERSATIONS_STORAGE_KEY, JSON.stringify(allConversations)); }
  function loadReads() { var raw = localStorage.getItem(READS_STORAGE_KEY); lastReadMap = raw ? JSON.parse(raw) : {}; }
  function saveReads() { localStorage.setItem(READS_STORAGE_KEY, JSON.stringify(lastReadMap)); }
  function getReadsForCurrentUser() {
    if (!currentUser) { return {}; }
    if (!lastReadMap[currentUser.id]) { lastReadMap[currentUser.id] = {}; }
    return lastReadMap[currentUser.id];
  }

  window.EtewaMessaging = {
    setCurrentUser: function (u) { currentUser = u; },
    findOrCreateConversationForTrip: function (trip, requester) {
      if (!currentUser) { return null; }
      var other = requester || {id:trip.ownerId, name:trip.traveler.name};
      if (!other.id || other.id === currentUser.id) { return null; }
      var existing = allConversations.find(function (c) {
        return c.tripId === String(trip.id) && c.participantIds.indexOf(currentUser.id) !== -1 && c.participantIds.indexOf(other.id) !== -1;
      });
      if (existing) { return existing; }
      var newConv = {
        id: 'conversation_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
        tripId: String(trip.id), tripRoute: trip.from + ' → ' + trip.to, tripDate: trip.date,
        participantIds: [currentUser.id, other.id],
        participants: [{ id: currentUser.id, name: currentUser.name }, { id: other.id, name: other.name }],
        createdAt: new Date().toISOString(),
        messages: requester ? [] : [{ id: 'message_' + Date.now(), authorId: currentUser.id, authorName: currentUser.name,
          text: 'Hi! I would like to send an item on your trip from ' + trip.from + ' to ' + trip.to + '.',
          sentAt: new Date().toISOString() }]
      };
      allConversations.push(newConv); saveConversations(); return newConv;
    },
    getConversationsForCurrentUser: function () {
      if (!currentUser) { return []; }
      return allConversations.filter(function (c) { return c.participantIds.indexOf(currentUser.id) !== -1; });
    },
    getConversationById: function (id) {
      return currentUser ? allConversations.find(function (c) { return c.id === id && c.participantIds.includes(currentUser.id); }) || null : null;
    },
    getOtherParticipantName: function (conv) {
      if (!currentUser) { return ''; }
      var other = conv.participants.find(function (p) { return p.id !== currentUser.id; });
      return other ? other.name : '';
    },
    addMessageToConversation: function (id, text) {
      if (!currentUser || !text.trim()) { return false; }
      var conv = allConversations.find(function (c) { return c.id === id; });
      if (!conv || conv.participantIds.indexOf(currentUser.id) === -1) { return false; }
      conv.messages.push({ id: 'message_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
        authorId: currentUser.id, authorName: currentUser.name, text: text.trim(), sentAt: new Date().toISOString() });
      saveConversations(); return true;
    },
    getUnreadCount: function (conv) {
      if (!currentUser) { return 0; }
      var reads = getReadsForCurrentUser();
      var lastReadAt = reads[conv.id] ? new Date(reads[conv.id]).getTime() : 0;
      return conv.messages.filter(function (m) {
        return m.authorId !== currentUser.id && new Date(m.sentAt).getTime() > lastReadAt;
      }).length;
    },
    markConversationRead: function (id) {
      if (!currentUser) { return; }
      var reads = getReadsForCurrentUser();
      reads[id] = new Date().toISOString();
      saveReads();
    }
  };
  loadConversations(); loadReads();
})();

/* ---------------- SHARED VALIDATION AND FORMATTING ---------------- */
var EtewaData = (function () {
  var regions = new Intl.DisplayNames(['en'], {type:'region'});
  var codes = document.currentScript.dataset.countryCodes.split(' ');
  var countries = codes.map(function (code) {
    return {code:code, name:regions.of(code)};
  }).sort(function (a,b) { return a.name.localeCompare(b.name); });
  var aliases = {UK:'GB', USA:'US', UAE:'AE', 'United States of America':'US', Turkey:'TR', 'Czech Republic':'CZ', 'Ivory Coast':'CI', 'South Korea':'KR', 'North Korea':'KP'};
  function normalize(value) {
    return String(value || '').trim().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  }
  function country(value, legacy) {
    var input = String(value || '').trim();
    if (legacy && input.includes(',')) { input = input.split(',').pop().trim(); }
    var key = normalize(input);
    var alias = Object.keys(aliases).find(function (name) { return normalize(name) === key; });
    return countries.find(function (item) { return normalize(item.name) === key || normalize(item.code) === key || (alias && aliases[alias] === item.code); });
  }
  function today(offset) {
    var date = new Date();
    date.setDate(date.getDate() + (offset || 0));
    return date.getFullYear() + '-' + String(date.getMonth()+1).padStart(2,'0') + '-' + String(date.getDate()).padStart(2,'0');
  }
  function positive(value) { return Number.isFinite(Number(value)) && Number(value) > 0; }
  function validPrice(value) { return positive(value) && /^(?:\d+(?:\.\d{0,2})?|\.\d{1,2})$/.test(String(value)); }
  function pricing(trip) {
    if (trip.pricing === 'fixed') { return 'Fixed Price · ' + money(trip.price); }
    if (trip.pricing === 'negotiable') { return 'Negotiable · suggested ' + money(trip.price); }
    return 'By Agreement';
  }
  function money(amount) { return new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'}).format(amount) + ' USD'; }
  function escape(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char];
    });
  }
  return {countries:countries, aliases:aliases, normalize:normalize, country:country, today:today,
    positive:positive, validPrice:validPrice, pricing:pricing, money:money, escape:escape};
})();

/* ---------------- 6. UI ---------------- */
document.addEventListener('DOMContentLoaded', function () {
  function q(s) { return document.querySelector(s); }
  function qa(s) { return document.querySelectorAll(s); }
  function initials(name) { return name.split(' ').map(function (w) { return w[0]; }).join('').slice(0, 2).toUpperCase(); }
  function formatJoinedDate(iso) { return new Date(iso).toLocaleDateString('en-US', { month:'short', year:'numeric' }); }
  function formatMessageTime(iso) { return new Date(iso).toLocaleTimeString('en-US', { hour:'2-digit', minute:'2-digit' }); }
  function formatTripDate(iso) { return new Date(iso + 'T00:00:00').toLocaleDateString('en-US', { month:'short', day:'numeric', year:'numeric' }); }
  function markInvalid(f) { if (f) { f.classList.add('invalid'); } return false; }
  function clearInvalid(scope) { qa(scope + ' .invalid').forEach(function (e) { e.classList.remove('invalid'); }); }
  function wireInvalidClearing() {
    qa('input, select, textarea').forEach(function (f) {
      f.addEventListener('input', function () { f.classList.remove('invalid'); });
      f.addEventListener('change', function () { f.classList.remove('invalid'); });
    });
  }

  var sampleTrips = [
    {id:1,from:'New York, USA',to:'Addis Ababa, Ethiopia',date:EtewaData.today(12),isSample:true,weight:3,traveler:{name:'Maya Thompson',rating:4.9,deliveries:128,verified:false}},
    {id:2,from:'Nairobi, Kenya',to:'Kampala, Uganda',date:EtewaData.today(09),isSample:true,weight:2,traveler:{name:'Brian Otieno',rating:4.7,deliveries:64,verified:false}},
    {id:3,from:'Dubai, UAE',to:'Istanbul, Turkey',date:EtewaData.today(15),isSample:true,weight:5,traveler:{name:'Layla Al-Farsi',rating:5.0,deliveries:203,verified:false}},
    {id:4,from:'London, UK',to:'Lagos, Nigeria',date:EtewaData.today(11),isSample:true,weight:4,traveler:{name:'Ade Okonkwo',rating:4.6,deliveries:41,verified:false}},
    {id:5,from:'Paris, France',to:'Dakar, Senegal',date:EtewaData.today(18),isSample:true,weight:1.5,traveler:{name:'Amara Diallo',rating:4.8,deliveries:92,verified:false}},
    {id:6,from:'Toronto, Canada',to:'Accra, Ghana',date:EtewaData.today(20),isSample:true,weight:2.5,traveler:{name:'Kwame Mensah',rating:4.9,deliveries:156,verified:false}},
    {id:7,from:'Johannesburg, South Africa',to:'Harare, Zimbabwe',date:EtewaData.today(13),isSample:true,weight:1,traveler:{name:'Thandeka Zulu',rating:4.5,deliveries:33,verified:false}},
    {id:8,from:'Cairo, Egypt',to:'Casablanca, Morocco',date:EtewaData.today(16),isSample:true,weight:6,traveler:{name:'Yusuf Hassan',rating:4.8,deliveries:77,verified:false}},
    {id:9,from:'Mumbai, India',to:'Dar es Salaam, Tanzania',date:EtewaData.today(14),isSample:true,weight:2,traveler:{name:'Priya Raman',rating:4.7,deliveries:58,verified:false}}
  ];

  var escapeHTML = EtewaData.escape;
  function wireCountryInput(input) {
    var options = document.createElement('ul');
    options.id = input.id + '-options';
    options.className = 'country-options';
    options.hidden = true;
    options.setAttribute('role', 'listbox');
    options.setAttribute('aria-label', input.labels[0].textContent);
    input.parentElement.classList.add('country-field');
    input.after(options);
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', options.id);
    input.setAttribute('aria-expanded', 'false');
    var matches = [], active = -1;
    function close() {
      options.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }
    function select(item) {
      input.value = item.name;
      input.classList.remove('invalid');
      close();
    }
    function show() {
      var query = EtewaData.normalize(input.value);
      matches = EtewaData.countries.filter(function (item) {
        return EtewaData.normalize(item.name).includes(query) || item.code.toLowerCase() === query ||
          Object.keys(EtewaData.aliases).some(function (alias) {
            return EtewaData.aliases[alias] === item.code && EtewaData.normalize(alias).startsWith(query);
          });
      });
      options.replaceChildren();
      active = -1;
      input.removeAttribute('aria-activedescendant');
      matches.forEach(function (item, index) {
        var option = document.createElement('li');
        option.id = options.id + '-' + index;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        option.textContent = item.name;
        option.addEventListener('mousedown', function (event) { event.preventDefault(); });
        option.addEventListener('click', function () { select(item); });
        options.appendChild(option);
      });
      options.hidden = !matches.length;
      input.setAttribute('aria-expanded', String(!options.hidden));
    }
    input.addEventListener('input', show);
    input.addEventListener('focus', show);
    input.addEventListener('blur', close);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' || event.key === 'Tab') { close(); return; }
      if (event.key === 'Enter' && !options.hidden && active >= 0) {
        event.preventDefault();
        select(matches[active]);
      }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (options.hidden) { show(); }
        if (!matches.length) { return; }
        if (active >= 0) { options.children[active].setAttribute('aria-selected','false'); }
        active = active < 0 ? (event.key === 'ArrowDown' ? 0 : matches.length - 1) :
          (active + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
        var option = options.children[active];
        option.setAttribute('aria-selected','true');
        input.setAttribute('aria-activedescendant', option.id);
        option.scrollIntoView({block:'nearest'});
      }
    });
  }
  function routeCountries(from, to, required) {
    var origin = EtewaData.country(from.value);
    var destination = EtewaData.country(to.value);
    var invalidFrom = !origin && (required || from.value.trim());
    var invalidTo = !destination && (required || to.value.trim());
    if (invalidFrom) { markInvalid(from); }
    if (invalidTo) { markInvalid(to); }
    if (invalidFrom || invalidTo) { return {error:'Choose a valid country for each route field you use.'}; }
    if (origin && destination && origin.code === destination.code) {
      markInvalid(from); markInvalid(to);
      return {error:'Departure and destination must be different countries.'};
    }
    if (origin) { from.value = origin.name; }
    if (destination) { to.value = destination.name; }
    return {from:origin, to:destination};
  }
  function routeMatches(value, selected) {
    var country = EtewaData.country(value, true);
    return !selected || (country && country.code === selected.code);
  }

  var categoryLabels = {documents:'Documents',electronics:'Electronics',clothing:'Clothing',small:'Small parcels',fragile:'Fragile items',other:'Other'};
  var searchState = {from:null,to:null,date:'',sort:'soonest'};
  var browseTrips = newTripPage();
  var myTrips = newTripPage();
  var myTripOwner = null;

  function newTripPage() {
    return {items:[], page:0, total:0, hasNext:false, loading:false, error:'', version:0};
  }

  async function loadTripPage(state, path, params, append, render) {
    if (append && (state.loading || !state.hasNext)) { return; }
    var version = ++state.version;
    if (!append) { Object.assign(state, {items:[], page:0, total:0, hasNext:false}); }
    state.loading = true;
    state.error = '';
    render();
    try {
      var result = await window.EtewaTrips.loadPage(path, Object.assign({}, params, {page:state.page + 1}));
      if (version !== state.version) { return; }
      var items = new Map(state.items.map(function (trip) { return [trip.id, trip]; }));
      result.items.forEach(function (trip) { items.set(trip.id, trip); });
      Object.assign(state, result, {items:Array.from(items.values())});
    } catch (error) {
      if (version === state.version) { state.error = error.message; }
    } finally {
      if (version === state.version) {
        state.loading = false;
        render();
      }
    }
  }

  function loadBrowseTrips(append) {
    var params = {sort:searchState.sort};
    if (searchState.from) { params.departure_country = searchState.from.code; }
    if (searchState.to) { params.destination_country = searchState.to.code; }
    if (searchState.date) { params.travel_date = searchState.date; }
    return loadTripPage(browseTrips, 'trips', params, append, renderTrips);
  }

  function loadMyTrips(append) {
    return loadTripPage(myTrips, 'my/trips', {}, append, renderMyTrips);
  }

  function filterTrips() {
    var localTrips = sampleTrips.concat(window.EtewaTrips.getLegacyTrips()).filter(function (trip) {
      return routeMatches(trip.from, searchState.from) && routeMatches(trip.to, searchState.to) &&
        (!searchState.date || trip.date >= searchState.date);
    });
    return browseTrips.items.concat(localTrips).sort(function (a,b) { return a.date.localeCompare(b.date); });
  }

  function setTripPageFeedback(selector, buttonSelector, state, loadingText, emptyText) {
    var feedback = q(selector), more = q(buttonSelector);
    var message = state.error || (state.loading ? loadingText : emptyText);
    setFormFeedback(feedback, message, Boolean(state.error));
    if (!state.error) { feedback.className = 'form-feedback'; }
    feedback.hidden = !message;
    more.hidden = !state.hasNext;
    more.disabled = state.loading;
  }

  function tripButtons(trip) {
    return (trip.status === 'published'
      ? '<button class="btn btn-ghost btn-sm" data-edit-trip-id="' + escapeHTML(trip.id) + '">Edit</button>' : '') +
      (trip.status !== 'archived'
        ? '<button class="btn btn-danger btn-sm" data-archive-trip-id="' + escapeHTML(trip.id) + '">Archive</button>' : '');
  }

  function findSharedTrip(id) {
    return myTrips.items.concat(browseTrips.items).find(function (trip) { return trip.id === id; });
  }

  function wireTripActions(container) {
    container.querySelectorAll('[data-edit-trip-id]').forEach(function (button) {
      button.addEventListener('click', function () { openTripModalForEdit(button.dataset.editTripId); });
    });
    container.querySelectorAll('[data-archive-trip-id]').forEach(function (button) {
      button.addEventListener('click', async function () {
        var trip = findSharedTrip(button.dataset.archiveTripId);
        if (!trip || !confirm('Archive this trip? It will be removed from public search.')) { return; }
        button.disabled = true;
        try {
          await window.EtewaTrips.archiveTrip(trip.databaseId);
          showAuthFeedback('Trip archived.', false);
          loadBrowseTrips(false);
          loadMyTrips(false);
        } catch (error) {
          showAuthFeedback(error.message, true);
        } finally {
          button.disabled = false;
        }
      });
    });
  }

  function renderTrips() {
    var grid = q('#tripGrid'), empty = q('#emptyState');
    var currentUser = window.EtewaAuth.getCurrentUser();
    var trips = filterTrips();
    var summary = !browseTrips.items.length && trips.length ? 'No shared trips match your search.' : '';
    if (searchState.sort === 'rating') {
      summary = ('Ratings are unavailable. Showing soonest departures. ' + summary).trim();
    }
    setTripPageFeedback('#tripBrowseFeedback', '#loadMoreTrips', browseTrips, 'Loading shared trips…', summary);
    grid.replaceChildren();
    empty.hidden = Boolean(trips.length || browseTrips.loading || browseTrips.error);
    trips.forEach(function (trip) {
      var isMine = currentUser && trip.ownerId === currentUser.id;
      var card = document.createElement('article');
      card.className = 'trip-card' + (isMine ? ' mine' : '');
      card.dataset.tripId = trip.id;
      var closed = trip.date < (trip.isShared ? utcToday() : EtewaData.today());
      var canInteractLocally = !trip.isShared && !trip.isSample && !closed && !isMine &&
        window.EtewaAuth.isServerAccountId(trip.ownerId);
      var canRequestShared = trip.isShared && !closed && !isMine && trip.availableGrams > 0;
      var source = trip.isShared ? 'Shared trip' : trip.isSample ? 'Sample trip' : 'Legacy browser trip';
      var secondary = trip.isShared ? trip.traveler.city : trip.isSample ? 'Sample traveler' : 'This browser only';
      card.innerHTML =
        '<div class="trip-route"><span>' + escapeHTML(trip.from) + '</span><span class="arrow">→</span><span>' + escapeHTML(trip.to) + '</span></div>' +
        '<div class="trip-meta">' +
          '<span class="chip primary">📅 ' + escapeHTML(formatTripDate(trip.date)) + '</span>' +
          '<span class="chip">' + escapeHTML(EtewaData.pricing(trip)) + '</span>' +
          '<span class="chip success">⚖️ ' + (trip.isShared ? escapeHTML(trip.availableGrams / 1000) + ' kg available' :
            'up to ' + escapeHTML(trip.weight) + ' kg') + '</span>' +
          (isMine ? '<span class="chip mine">Your trip</span>' : '') +
          '<span class="chip">' + source + '</span>' +
          (closed ? '<span class="chip">Departed</span>' : '') +
        '</div>' +
        '<div class="trip-traveler"><div class="avatar">' + escapeHTML(initials(trip.traveler.name)) + '</div>' +
          '<div class="traveler-info"><strong>' + escapeHTML(trip.traveler.name) + '</strong>' +
          (secondary ? '<span>' + escapeHTML(secondary) + '</span>' : '') + '</div></div>' +
        (trip.notes ? '<p class="trip-notes">' + escapeHTML(trip.notes) + '</p>' : '') +
        '<div class="trip-footer">' +
          (isMine && trip.isShared ? tripButtons(trip) :
            canRequestShared ? '<button class="btn btn-primary btn-sm" data-request-trip-id="' + escapeHTML(trip.id) + '">Request Delivery</button>' :
            canInteractLocally ? '<button class="btn btn-primary btn-sm" data-request-trip-id="' + escapeHTML(trip.id) + '">Request locally</button>' +
            '<button class="btn btn-ghost btn-sm" data-message-trip-id="' + escapeHTML(trip.id) + '">Message locally</button>' : '') +
        '</div>';
      grid.appendChild(card);
    });
    grid.querySelectorAll('[data-request-trip-id]').forEach(function (button) {
      button.addEventListener('click', function () { openRequestModal(button.dataset.requestTripId); });
    });
    grid.querySelectorAll('[data-message-trip-id]').forEach(function (button) {
      button.addEventListener('click', function () { startConversation(button.dataset.messageTripId); });
    });
    wireTripActions(grid);
  }

  /* ---------- REQUEST MODAL ---------- */
  var pendingRequestTrip = null;

  function openRequestModal(tripId) {
    var user = window.EtewaAuth.getCurrentUser();
    if (!user) { q('#loginModal').hidden = false; return; }
    var trip = findSharedTrip(tripId) || window.EtewaTrips.findLegacyTrip(tripId);
    if (!trip || trip.ownerId === user.id || !window.EtewaAuth.isServerAccountId(trip.ownerId) ||
        trip.date < (trip.isShared ? utcToday() : EtewaData.today()) || (trip.isShared && !trip.availableGrams)) { return; }
    pendingRequestTrip = trip;
    var capacity = trip.isShared ? trip.availableGrams / 1000 : window.EtewaRequests.remainingCapacity(trip);
    q('#requestModalSummary').textContent = trip.from + ' → ' + trip.to + ' · ' + formatTripDate(trip.date) + ' · with ' +
      trip.traveler.name + ' · ' + EtewaData.pricing(trip) + ' · ' + capacity + ' kg available';
    var pricing = q('#requestPricingInfo');
    pricing.hidden = false;
    pricing.textContent = !trip.isShared ? 'This request is saved in this browser only.' :
      trip.pricing === 'fixed' ? 'The listed price applies. No payment is collected here.' :
      trip.pricing === 'negotiable' ? 'The listed price is a suggestion. Sending a request does not create a price agreement.' :
      'There is no initial price. Price agreements are not handled here yet.';
    q('#deliveryRequestForm').reset();
    q('#deliveryOfferField').hidden = trip.isShared || trip.pricing === 'fixed';
    q('#deliveryOtherExplanationField').classList.remove('visible');
    q('#deliveryRequestFeedback').textContent = '';
    clearInvalid('#deliveryRequestForm');
    q('#requestModal').hidden = false;
  }
  q('#deliveryCategory').addEventListener('change', function (event) {
    var field = q('#deliveryOtherExplanationField');
    field.classList.toggle('visible', event.target.value === 'other');
    if (event.target.value !== 'other') { q('#deliveryOtherExplanation').value = ''; }
  });
  q('#deliveryRequestForm').addEventListener('submit', async function (event) {
    event.preventDefault();
    var button = event.currentTarget.querySelector('button[type="submit"]');
    var feedback = q('#deliveryRequestFeedback'), trip = pendingRequestTrip;
    if (button.disabled || !trip) { return; }
    var category = q('#deliveryCategory'), weight = q('#deliveryWeight'), description = q('#deliveryDescription');
    var details = q('#deliveryOtherExplanation'), offer = q('#deliveryOffer');
    clearInvalid('#deliveryRequestForm');
    var grams = wholeUnits(weight.value, 3);
    var ok = true;
    if (!category.value) { ok = markInvalid(category); }
    if (grams === null) { ok = markInvalid(weight); }
    if (!description.value.trim()) { ok = markInvalid(description); }
    if (category.value === 'other' && !details.value.trim()) { ok = markInvalid(details); }
    if (!q('#deliveryOfferField').hidden && (offer.validity.badInput || (offer.value && !EtewaData.validPrice(offer.value)))) {
      ok = markInvalid(offer);
    }
    if (!ok) { setFormFeedback(feedback, 'Complete the highlighted fields. Use a positive weight with up to three decimal places.', true); return; }
    var capacity = trip.isShared ? trip.availableGrams / 1000 : window.EtewaRequests.remainingCapacity(trip);
    if (grams / 1000 > capacity) {
      markInvalid(weight);
      setFormFeedback(feedback, 'The package exceeds the remaining capacity for this trip.', true);
      return;
    }
    button.disabled = true;
    setFormFeedback(feedback, 'Sending request…', false);
    try {
      if (trip.isShared) {
        await window.EtewaAuth.request('trips/' + trip.databaseId + '/requests', 'POST', {
          package_category:category.value, category_details:category.value === 'other' ? details.value : null,
          package_description:description.value, weight_grams:grams
        });
      } else {
        var saved = window.EtewaRequests.createRequest(trip, {category:category.value, otherExplanation:details.value.trim(),
          weight:grams / 1000, description:description.value.trim(), proposedPrice:!q('#deliveryOfferField').hidden && offer.value ? Number(offer.value) : null});
        if (!saved) { throw new Error('This trip is unavailable or you already have an active request.'); }
      }
      q('#requestModal').hidden = true;
      pendingRequestTrip = null;
      if (trip.isShared) { loadDashboardRequests('Delivery request sent.'); }
      else { renderRequests(); showRequestFeedback('Delivery request saved in this browser.', false); }
      q('#myRequests').scrollIntoView({behavior:'smooth'});
    } catch (error) {
      setFormFeedback(feedback, error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  /* ---------- CONVERSATION ---------- */
  var activeConversationId = null;
  function startConversation(tripId) {
    if (!window.EtewaAuth.getCurrentUser()) { alert('Please log in first to message travelers.'); q('#loginModal').hidden = false; return; }
    var trip = window.EtewaTrips.findLegacyTrip(tripId);
    if (!trip || !window.EtewaAuth.isServerAccountId(trip.ownerId)) { return; }
    var conv = window.EtewaMessaging.findOrCreateConversationForTrip(trip);
    if (!conv) { alert('You cannot message your own trip.'); return; }
    showConversation(conv);
  }
  function showConversation(conv) {
    renderMessaging();
    openThread(conv.id);
    q('#messages').scrollIntoView({behavior:'smooth'});
  }

  function renderMessaging() {
    var currentUser = window.EtewaAuth.getCurrentUser();
    var section = q('#messages');
    var active = window.EtewaMessaging.getConversationById(activeConversationId);
    if (!active) {
      activeConversationId = null;
      q('#conversationListView').hidden = false;
      q('#conversationThreadView').hidden = true;
      q('#messageList').replaceChildren();
      q('#messageInput').value = '';
    }
    if (!currentUser) { section.hidden = true; q('#conversationList').replaceChildren(); return; }
    section.hidden = false;
    var list = q('#conversationList'), empty = q('#conversationEmptyState');
    var conversations = window.EtewaMessaging.getConversationsForCurrentUser();
    list.innerHTML = '';
    if (conversations.length === 0) { empty.hidden = false; return; }
    empty.hidden = true;
    conversations.forEach(function (conv) {
      var otherName = window.EtewaMessaging.getOtherParticipantName(conv);
      var last = conv.messages[conv.messages.length - 1];
      var unread = window.EtewaMessaging.getUnreadCount(conv);
      var item = document.createElement('div');
      item.className = 'conversation-item' + (unread > 0 ? ' has-unread' : '');
      item.innerHTML =
        '<span class="notification-dot"></span>' +
        '<div class="avatar">' + escapeHTML(initials(otherName)) + '</div>' +
        '<div class="conversation-item-info"><strong>' + escapeHTML(otherName) + '</strong>' +
          '<span>' + escapeHTML(conv.tripRoute) + ' · ' + escapeHTML((last ? last.text : '')) + '</span></div>' +
        '<div class="conversation-item-time">' +
          '<span>' + escapeHTML((last ? formatMessageTime(last.sentAt) : '')) + '</span>' +
          (unread > 0 ? '<span class="unread-badge">' + unread + '</span>' : '') +
        '</div>';
      item.addEventListener('click', function () { window.EtewaMessaging.markConversationRead(conv.id); openThread(conv.id); });
      list.appendChild(item);
    });
  }

  function openThread(convId) {
    var conv = window.EtewaMessaging.getConversationById(convId);
    if (!conv) { return; }
    activeConversationId = convId;
    window.EtewaMessaging.markConversationRead(convId);
    q('#conversationListView').hidden = true;
    q('#conversationThreadView').hidden = false;
    var currentUser = window.EtewaAuth.getCurrentUser();
    q('#threadTravelerName').textContent = window.EtewaMessaging.getOtherParticipantName(conv);
    q('#threadRoute').textContent = conv.tripRoute + ' · ' + formatTripDate(conv.tripDate);
    var list = q('#messageList');
    list.innerHTML = '';
    conv.messages.forEach(function (msg) {
      var mine = currentUser && msg.authorId === currentUser.id;
      var bubble = document.createElement('div');
      bubble.className = 'message-bubble ' + (mine ? 'from-me' : 'from-other');
      bubble.textContent = msg.text;
      var meta = document.createElement('small');
      meta.textContent = msg.authorName + ' · ' + formatMessageTime(msg.sentAt);
      bubble.appendChild(meta);
      list.appendChild(bubble);
    });
    list.scrollTop = list.scrollHeight;
  }

  q('#backToConversationList').addEventListener('click', function () {
    activeConversationId = null;
    q('#conversationThreadView').hidden = true;
    q('#conversationListView').hidden = false;
    renderMessaging();
  });
  q('#messageComposeForm').addEventListener('submit', function (e) {
    e.preventDefault();
    var input = q('#messageInput');
    if (!input.value.trim() || !activeConversationId) { return; }
    window.EtewaMessaging.addMessageToConversation(activeConversationId, input.value.trim());
    input.value = '';
    openThread(activeConversationId);
  });

  /* ---------- REQUESTS LISTS ---------- */
  var sharedRequests = {incoming:[], outgoing:[]};
  var requestLoadVersion = 0, requestsLoading = false, requestsLoadFailed = false;

  function showRequestFeedback(message, error, incoming) {
    var feedback = q(incoming ? '#incomingRequestsFeedback' : '#requestsFeedback');
    setFormFeedback(feedback, message, error);
    feedback.hidden = !message;
  }

  async function loadDashboardRequests(notice, incoming) {
    if (!window.EtewaAuth.getCurrentUser()) { return; }
    var version = ++requestLoadVersion;
    requestsLoading = true;
    requestsLoadFailed = false;
    showRequestFeedback('', false, !incoming);
    showRequestFeedback('Loading requests…', false, incoming);
    q('#refreshRequests').disabled = true;
    renderRequests();
    try {
      var result = await window.EtewaAuth.request('my/requests', 'GET');
      if (version !== requestLoadVersion) { return; }
      sharedRequests = result;
      showRequestFeedback(notice || '', false, incoming);
    } catch (error) {
      if (version === requestLoadVersion) {
        requestsLoadFailed = true;
        showRequestFeedback(notice ? notice + ' Refresh requests to update the list.' : error.message, true, incoming);
      }
    } finally {
      if (version === requestLoadVersion) {
        requestsLoading = false;
        q('#refreshRequests').disabled = false;
        renderRequests();
        renderDashboard();
      }
    }
  }

  function sharedRequestItem(item) {
    var trip = item.trip;
    return {
      shared:true, id:item.id, status:item.status, actions:item.allowed_actions, acceptNotice:item.accept_notice,
      senderName:item.requester.full_name, senderProfile:{name:item.requester.full_name, city:item.requester.city},
      travelerName:trip.traveler.full_name, tripFrom:EtewaData.country(trip.departure_country)?.name || trip.departure_country,
      tripTo:EtewaData.country(trip.destination_country)?.name || trip.destination_country, tripDate:trip.travel_date,
      category:item.package_category, otherExplanation:item.category_details, description:item.package_description,
      weight:item.weight_grams / 1000, pricing:trip.pricing_method, tripPrice:trip.price_minor === null ? null : trip.price_minor / 100
    };
  }

  function renderRequests() {
    var currentUser = window.EtewaAuth.getCurrentUser();
    if (!currentUser) { q('#incomingRequestsList').innerHTML = ''; q('#outgoingRequestsList').innerHTML = ''; return; }
    renderRequestList(sharedRequests.incoming.map(sharedRequestItem).concat(window.EtewaRequests.getRequestsForTripsOwnedByCurrentUser()),
      '#incomingRequestsList', '#incomingRequestsEmptyState', true);
    renderRequestList(sharedRequests.outgoing.map(sharedRequestItem).concat(window.EtewaRequests.getRequestsSentByCurrentUser()),
      '#outgoingRequestsList', '#outgoingRequestsEmptyState', false);
  }
  function renderRequestList(requests, listSel, emptySel, incoming) {
    var list = q(listSel), empty = q(emptySel);
    list.innerHTML = '';
    if (requests.length === 0) { empty.hidden = requestsLoading || requestsLoadFailed; return; }
    empty.hidden = true;
    requests.forEach(function (req) {
      var label = req.status === 'pending' ? 'Pending' : req.status === 'declined' ? 'Declined' :
        window.EtewaRequests.getStatusLabel(req.status);
      var catText = req.category === 'other' && req.otherExplanation ? 'Other: ' + req.otherExplanation : (categoryLabels[req.category] || req.category);
      var actions = '';
      if (req.shared) {
        var labels = {accept:'Accept', decline:'Decline', cancel:'Cancel'};
        req.actions.forEach(function (action) {
          actions += '<button class="btn ' + (action === 'accept' ? 'btn-success' : 'btn-ghost') +
            ' btn-sm" data-shared-action="' + escapeHTML(action) + '">' + labels[action] + '</button>';
        });
      } else if (incoming) {
        if (req.status === 'requested') {
          actions = '<button class="btn btn-success btn-sm" data-action="accepted" data-request-id="' + escapeHTML(req.id) + '">Accept</button>' +
                    '<button class="btn btn-danger btn-sm" data-action="cancelled" data-request-id="' + escapeHTML(req.id) + '">Decline</button>';
        } else if (req.status === 'accepted') {
          actions = '<button class="btn btn-primary btn-sm" data-action="in_transit" data-request-id="' + escapeHTML(req.id) + '">Mark In Transit</button>';
        } else if (req.status === 'in_transit') {
          actions = '<button class="btn btn-success btn-sm" data-action="delivered" data-request-id="' + escapeHTML(req.id) + '">Mark Delivered</button>';
        }
      } else if (req.status === 'requested' || req.status === 'accepted') {
        actions = '<button class="btn btn-ghost btn-sm" data-action="cancelled" data-request-id="' + escapeHTML(req.id) + '">Cancel</button>';
      }
      var profile = incoming && req.senderProfile ? req.senderProfile : null;
      var counterpart = incoming ? 'From ' + (profile ? profile.name : req.senderName) : 'To ' + req.travelerName;
      var priceText = EtewaData.pricing({pricing:req.pricing, price:req.tripPrice});
      if (req.proposedPrice) { priceText += ' · Proposed ' + EtewaData.money(req.proposedPrice); }
      if (!req.shared && incoming && window.EtewaAuth.isServerAccountId(req.senderId)) {
        actions += '<button class="btn btn-ghost btn-sm" data-message-requester>Message locally</button>';
      }
      var item = document.createElement('div');
      item.className = 'request-item';
      if (req.shared) { item.dataset.sharedRequestId = req.id; }
      item.innerHTML =
        '<div class="request-item-info"><strong>' + escapeHTML(req.tripFrom) + ' → ' + escapeHTML(req.tripTo) + '</strong>' +
          '<span>' + escapeHTML(counterpart) + ' · ' + escapeHTML(catText) + ' · ' + escapeHTML(req.weight) + ' kg · ' + escapeHTML(formatTripDate(req.tripDate)) + '</span>' +
          '<p class="request-description">' + escapeHTML(req.description || '') + '</p>' +
          (profile && (profile.city || profile.bio) ? '<p class="request-profile">' +
            escapeHTML([profile.city, profile.bio].filter(Boolean).join(' · ')) + '</p>' : '') +
          '<span>' + escapeHTML(priceText) + '</span>' +
          (req.acceptNotice ? '<p class="request-description">' + escapeHTML(req.acceptNotice) + '</p>' : '') +
          (req.shared ? '' : '<span> · Browser only</span>') +
          '<div style="margin-top:6px"><span class="chip status-' + escapeHTML(req.status) + '">' + escapeHTML(label) + '</span></div></div>' +
        '<div class="request-item-actions">' + actions + '</div>';
      item.querySelectorAll('[data-shared-action]').forEach(function (button) {
        button.addEventListener('click', async function () {
          var buttons = item.querySelectorAll('[data-shared-action]');
          buttons.forEach(function (control) { control.disabled = true; });
          try {
            var result = await window.EtewaAuth.request('requests/' + req.id + '/' + button.dataset.sharedAction, 'POST');
            var direction = incoming ? 'incoming' : 'outgoing';
            sharedRequests[direction] = sharedRequests[direction].map(function (row) {
              return row.id === result.request.id ? result.request : row;
            });
            renderRequests();
            loadDashboardRequests('Request ' + result.request.status + '.', incoming);
            loadBrowseTrips(false);
          } catch (error) {
            showRequestFeedback(error.message, true, incoming);
          } finally {
            buttons.forEach(function (control) { control.disabled = false; });
          }
        });
      });
      var messageButton = item.querySelector('[data-message-requester]');
      if (messageButton) {
        messageButton.addEventListener('click', function () {
          var trip = {id:req.tripId, from:req.tripFrom, to:req.tripTo, date:req.tripDate};
          var conversation = window.EtewaMessaging.findOrCreateConversationForTrip(trip, {id:req.senderId, name:profile ? profile.name : req.senderName});
          if (conversation) { showConversation(conversation); }
        });
      }
      list.appendChild(item);
    });
    list.querySelectorAll('[data-action]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (window.EtewaRequests.updateRequestStatus(btn.getAttribute('data-request-id'), btn.getAttribute('data-action'))) {
          refreshAll();
        } else { alert('Cannot change status. Check the trip date and remaining capacity.'); }
      });
    });
  }

  /* ---------- HEADER / DASHBOARD / MY TRIPS ---------- */
  function renderHeader() {
    var actions = q('#headerActions');
    var currentUser = window.EtewaAuth.getCurrentUser();
    q('#navMyRequests').hidden = !currentUser;
    if (!currentUser) {
      actions.innerHTML = '<button class="btn btn-ghost" id="openLoginButton">Log In</button>' +
                          '<button class="btn btn-primary" id="openRegisterButton">Sign Up</button>';
      q('#openLoginButton').addEventListener('click', function () { q('#loginModal').hidden = false; });
      q('#openRegisterButton').addEventListener('click', function () { q('#registerModal').hidden = false; });
    } else {
      actions.innerHTML = '<button class="btn btn-ghost" id="openDashboardButton">Dashboard</button>' +
        '<div class="header-user"><div class="avatar">' + escapeHTML(initials(currentUser.name)) + '</div>' +
        '<span class="header-user-name">' + escapeHTML(currentUser.name) + '</span></div>' +
        '<button class="btn btn-ghost" id="headerLogoutButton">Log Out</button>';
      q('#openDashboardButton').addEventListener('click', function () { document.getElementById('dashboard').scrollIntoView({ behavior: 'smooth' }); });
      q('#headerLogoutButton').addEventListener('click', logOut);
    }
  }
  function renderDashboard() {
    var currentUser = window.EtewaAuth.getCurrentUser();
    var section = document.getElementById('dashboard');
    if (!currentUser) { section.hidden = true; return; }
    section.hidden = false;
    q('#dashboardAvatar').textContent = initials(currentUser.name);
    q('#dashboardGreeting').textContent = 'Welcome back, ' + currentUser.name.split(' ')[0];
    q('#dashboardSub').textContent = (currentUser.city ? currentUser.city + ' · ' : '') + currentUser.email;
    q('#dashboardDeliveriesCount').textContent = window.EtewaRequests.countCompleted(currentUser.id) +
      sharedRequests.incoming.filter(function (item) { return item.status === 'delivered'; }).length;
    q('#dashboardRating').textContent = currentUser.averageRating === null ? '—' : currentUser.averageRating;
    q('#dashboardJoined').textContent = formatJoinedDate(currentUser.joinedAt);
  }
  function renderMyTrips() {
    var list = q('#myTripsList'), empty = q('#myTripsEmptyState');
    var currentUser = window.EtewaAuth.getCurrentUser();
    list.replaceChildren();
    if (!currentUser) { return; }
    q('#dashboardTripsCount').textContent = myTrips.loading && !myTrips.items.length ? '—' : myTrips.total;
    setTripPageFeedback('#myTripsFeedback', '#loadMoreMyTrips', myTrips, 'Loading your trips…', '');
    var legacyTrips = window.EtewaTrips.getLegacyTrips().filter(function (trip) {
      return trip.ownerId === currentUser.id;
    });
    var trips = myTrips.items.concat(legacyTrips);
    empty.hidden = Boolean(trips.length || myTrips.loading || myTrips.error);
    trips.forEach(function (trip) {
      var item = document.createElement('div');
      item.className = 'my-trip-item';
      item.dataset.tripId = trip.id;
      var status = !trip.isShared ? 'Legacy browser trip' :
        trip.status === 'published' ? (trip.date < utcToday() ? 'Departed' : 'Published') :
        trip.status[0].toUpperCase() + trip.status.slice(1);
      item.innerHTML = '<div class="my-trip-item-info"><strong>' + escapeHTML(trip.from) + ' → ' + escapeHTML(trip.to) + '</strong>' +
        '<span>' + escapeHTML(formatTripDate(trip.date)) + ' · ' + escapeHTML(EtewaData.pricing(trip)) + ' · up to ' +
        escapeHTML(trip.weight) + ' kg · ' + escapeHTML(status) + '</span></div>' +
        '<div class="my-trip-item-actions">' + (trip.isShared ? tripButtons(trip) : '') + '</div>';
      list.appendChild(item);
    });
    wireTripActions(list);
  }

  /* ---------- TRIP MODAL ---------- */
  function openTripModalForCreate() {
    q('#tripModalTitle').textContent = 'Publish a Trip';
    q('#tripSubmitButton').textContent = 'Publish Trip';
    q('#tripEditingId').value = '';
    q('#tripForm').reset();
    updateTripPriceField();
    q('#tripDate').min = utcToday();
    q('#tripFeedback').textContent = '';
    clearInvalid('#tripForm');
    q('#tripModal').hidden = false;
  }
  function openTripModalForEdit(tripId) {
    var trip = findSharedTrip(tripId), user = window.EtewaAuth.getCurrentUser();
    if (!trip || !user || trip.ownerId !== user.id || trip.status !== 'published') { return; }
    q('#tripModalTitle').textContent = 'Edit Trip';
    q('#tripSubmitButton').textContent = 'Save Changes';
    q('#tripEditingId').value = trip.id;
    var origin = EtewaData.country(trip.from, true), destination = EtewaData.country(trip.to, true);
    q('#tripFrom').value = origin ? origin.name : trip.from;
    q('#tripTo').value = destination ? destination.name : trip.to;
    q('#tripDate').min = utcToday();
    q('#tripDate').value = trip.date; q('#tripWeight').value = trip.weight;
    q('#tripPricing').value = trip.pricing || 'agreement';
    q('#tripPrice').value = trip.price || '';
    q('#tripNotes').value = trip.notes || '';
    updateTripPriceField();
    q('#tripFeedback').textContent = '';
    clearInvalid('#tripForm');
    q('#tripModal').hidden = false;
  }
  function updateTripPriceField() {
    var method = q('#tripPricing').value;
    q('#tripPriceField').hidden = method === 'agreement';
    q('#tripPrice').required = method !== 'agreement';
    q('#tripPrice').classList.remove('invalid');
    q('#tripPriceLabel').textContent = method === 'negotiable' ? 'Suggested price (USD) *' : 'Fixed price (USD) *';
  }
  q('#tripPricing').addEventListener('change', updateTripPriceField);
  function utcToday() {
    return new Date().toISOString().slice(0, 10);
  }

  function wholeUnits(value, places) {
    var parts = /^(?:(\d+)(?:\.(\d*))?|\.(\d+))$/.exec(value);
    if (!parts) { return null; }
    var fraction = parts[2] || parts[3] || '';
    if (fraction.length > places) { return null; }
    var amount = Number((parts[1] || '0') + fraction.padEnd(places, '0'));
    return Number.isSafeInteger(amount) && amount > 0 ? amount : null;
  }

  q('#tripForm').addEventListener('submit', async function (event) {
    event.preventDefault();
    var button = q('#tripSubmitButton'), feedback = q('#tripFeedback');
    if (button.disabled) { return; }
    var origin = q('#tripFrom'), destination = q('#tripTo'), date = q('#tripDate'), weight = q('#tripWeight');
    var price = q('#tripPrice'), method = q('#tripPricing').value;
    var editingId = q('#tripEditingId').value;
    var existing = editingId ? findSharedTrip(editingId) : null;
    if (editingId && !existing) {
      setFormFeedback(feedback, 'This trip changed. Reopen it and try again.', true);
      return;
    }
    clearInvalid('#tripForm');
    var route = routeCountries(origin, destination, true);
    var capacityGrams = wholeUnits(weight.value, 3);
    var priceMinor = method === 'agreement' ? null : wholeUnits(price.value, 2);
    var ok = !route.error;
    if (!date.value || date.value < utcToday()) { ok = markInvalid(date); }
    if (capacityGrams === null) { ok = markInvalid(weight); }
    if (method !== 'agreement' && priceMinor === null) { ok = markInvalid(price); }
    if (!ok) {
      setFormFeedback(feedback, route.error ||
        'Use today or a future date, a positive capacity in whole grams, and a positive USD price in whole cents when required.', true);
      return;
    }
    var details = {departure_country:route.from.code, destination_country:route.to.code, travel_date:date.value,
      capacity_grams:capacityGrams, pricing_method:method, price_minor:priceMinor, currency:'USD', notes:q('#tripNotes').value.trim()};
    button.disabled = true;
    setFormFeedback(feedback, 'Saving trip…', false);
    try {
      if (existing) { await window.EtewaTrips.updateTrip(existing.databaseId, details); }
      else { await window.EtewaTrips.publishTrip(details); }
      q('#tripModal').hidden = true;
      showAuthFeedback(existing ? 'Trip updated.' : 'Trip published.', false);
      loadBrowseTrips(false);
      loadMyTrips(false);
    } catch (error) {
      setFormFeedback(feedback, error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  /* ---------- AUTH FORMS ---------- */
  function showAuthFeedback(message, error) {
    var feedback = q('#authFeedback');
    feedback.textContent = message;
    feedback.className = 'container form-feedback ' + (error ? 'error' : 'success');
    feedback.hidden = !message;
  }

  function setFormFeedback(feedback, message, error) {
    feedback.textContent = message;
    feedback.className = 'form-feedback ' + (error ? 'error' : 'success');
  }

  async function submitAccountForm(form, feedback, operation) {
    var button = form.querySelector('button[type="submit"]');
    if (button.disabled) { return; }
    button.disabled = true;
    setFormFeedback(feedback, '', false);
    try {
      await operation();
    } catch (error) {
      setFormFeedback(feedback, error.message, true);
      if (!window.EtewaAuth.getCurrentUser() && !q('#profileModal').hidden) {
        q('#profileForm').reset();
        q('#profileModal').hidden = true;
        refreshAll();
        showAuthFeedback(error.message, true);
      }
    } finally {
      button.disabled = false;
    }
  }

  function signedIn(form, modal, message) {
    form.reset();
    q(modal).hidden = true;
    refreshAll();
    showAuthFeedback(message, false);
    q('#dashboard').scrollIntoView({behavior:'smooth'});
  }

  q('#registerForm').addEventListener('submit', function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    var name = q('#registerName'), email = q('#registerEmail'), password = q('#registerPassword');
    clearInvalid('#registerForm');
    var feedback = q('#registerFeedback');
    submitAccountForm(form, feedback, async function () {
      await window.EtewaAuth.register(name.value, email.value, password.value, q('#registerCity').value);
      signedIn(form, '#registerModal', 'Account created. You’re signed in.');
    });
  });

  q('#loginForm').addEventListener('submit', function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    submitAccountForm(form, q('#loginFeedback'), async function () {
      await window.EtewaAuth.login(q('#loginEmail').value, q('#loginPassword').value);
      signedIn(form, '#loginModal', 'Signed in.');
    });
  });

  async function logOut() {
    var button = q('#headerLogoutButton'), dashboardButton = q('#dashboardLogout');
    if (!button || button.disabled) { return; }
    button.disabled = true;
    dashboardButton.disabled = true;
    try {
      await window.EtewaAuth.logout();
      ['#profileModal','#tripModal','#requestModal'].forEach(function (selector) { q(selector).hidden = true; });
      q('#profileForm').reset();
      refreshAll();
      showAuthFeedback('Signed out.', false);
    } catch (error) {
      showAuthFeedback(error.message, true);
    } finally {
      button.disabled = false;
      dashboardButton.disabled = false;
    }
  }

  /* ---------- PROFILE ---------- */
  async function openProfileModal() {
    var button = q('#dashboardEditProfile');
    if (button.disabled) { return; }
    button.disabled = true;
    try {
      var profile = await window.EtewaAuth.getProfile();
      q('#profileName').value = profile.name;
      q('#profileCity').value = profile.city;
      q('#profilePhone').value = profile.phone;
      q('#profileBio').value = profile.bio;
      setFormFeedback(q('#profileFeedback'), '', false);
      q('#profileModal').hidden = false;
    } catch (error) {
      refreshAll();
      showAuthFeedback(error.message, true);
    } finally {
      button.disabled = false;
    }
  }
  q('#profileForm').addEventListener('submit', function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    var feedback = q('#profileFeedback');
    submitAccountForm(form, feedback, async function () {
      await window.EtewaAuth.updateProfile({
        full_name:q('#profileName').value, city:q('#profileCity').value,
        phone:q('#profilePhone').value, bio:q('#profileBio').value
      });
      setFormFeedback(feedback, 'Profile saved.', false);
      refreshAll();
      loadBrowseTrips(false);
    });
  });

  /* ---------- MODAL CLOSE ---------- */
  function wireClose(attr, sel) {
    qa('[' + attr + ']').forEach(function (el) { el.addEventListener('click', function () { q(sel).hidden = true; }); });
  }
  wireClose('data-close-register', '#registerModal');
  wireClose('data-close-login', '#loginModal');
  wireClose('data-close-profile', '#profileModal');
  wireClose('data-close-trip', '#tripModal');
  wireClose('data-close-request', '#requestModal');

  /* ---------- BUTTONS ---------- */
  q('#homePublishTrip').addEventListener('click', function () {
    if (window.EtewaAuth.getCurrentUser()) { openTripModalForCreate(); }
    else { q('#loginModal').hidden = false; }
  });
  q('#homeSendPackage').addEventListener('click', function (event) {
    event.preventDefault();
    q('#searchForm').scrollIntoView({behavior:'smooth'});
    q('#from').focus({preventScroll:true});
  });
  q('#navMyRequests').addEventListener('click', function () {
    q('#mainNav').classList.remove('open');
    loadDashboardRequests();
  });
  q('#refreshRequests').addEventListener('click', function () { loadDashboardRequests(); loadBrowseTrips(false); });
  q('#dashboardPublishTrip').addEventListener('click', openTripModalForCreate);
  q('#dashboardEditProfile').addEventListener('click', openProfileModal);
  q('#dashboardLogout').addEventListener('click', logOut);
  q('#menuToggle').addEventListener('click', function () { q('#mainNav').classList.toggle('open'); });
  q('#year').textContent = new Date().getFullYear();

  /* ---------- SEARCH + SORT ---------- */
  q('#searchForm').addEventListener('submit', function (e) {
    e.preventDefault();
    clearInvalid('#searchForm');
    var route = routeCountries(q('#from'), q('#to'), false);
    q('#searchFeedback').textContent = route.error || '';
    q('#searchFeedback').hidden = !route.error;
    if (route.error) { return; }
    searchState.from = route.from;
    searchState.to = route.to;
    searchState.date = q('#date').value;
    loadBrowseTrips(false);
    document.getElementById('browse').scrollIntoView({ behavior: 'smooth' });
  });
  q('#filterSort').addEventListener('change', function (event) {
    searchState.sort = event.target.value;
    loadBrowseTrips(false);
  });
  q('#loadMoreTrips').addEventListener('click', function () { loadBrowseTrips(true); });
  q('#loadMoreMyTrips').addEventListener('click', function () { loadMyTrips(true); });

  /* ---------- REFRESH ---------- */
  function refreshAll() {
    var currentUser = window.EtewaAuth.getCurrentUser();
    if (window.EtewaMessaging && window.EtewaMessaging.setCurrentUser) { window.EtewaMessaging.setCurrentUser(currentUser); }
    if (window.EtewaRequests && window.EtewaRequests.setCurrentUser) { window.EtewaRequests.setCurrentUser(currentUser); }
    var ownerId = currentUser ? currentUser.id : null;
    if (ownerId !== myTripOwner) {
      myTripOwner = ownerId;
      myTrips.version++;
      myTrips = newTripPage();
      sharedRequests = {incoming:[], outgoing:[]};
      requestLoadVersion++;
      requestsLoading = false;
      requestsLoadFailed = false;
      q('#refreshRequests').disabled = false;
      showRequestFeedback('', false, true);
      showRequestFeedback('', false, false);
      if (ownerId) { loadMyTrips(false); loadDashboardRequests(); }
    }
    renderHeader(); renderDashboard(); renderMyTrips(); renderMessaging(); renderRequests(); renderTrips();
  }

  ['#from','#to','#tripFrom','#tripTo'].forEach(function (selector) { wireCountryInput(q(selector)); });
  wireInvalidClearing();
  refreshAll();
  loadBrowseTrips(false);
  window.EtewaAuth.restore().then(refreshAll).catch(function (error) {
    showAuthFeedback(error.message, true);
  });
});
