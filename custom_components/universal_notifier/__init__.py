# /config/custom_components/universal_notifier/__init__.py

"""Universal Notifier Component: wrapper avanzato per notifiche e assistenti vocali."""
import logging
import asyncio
import random
import voluptuous as vol
import homeassistant.util.dt as dt_util
from datetime import time

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN, CONF_CHANNELS, CONF_ASSISTANT_NAME, CONF_DATE_FORMAT, 
    CONF_GREETINGS, CONF_IS_VOICE, CONF_OVERRIDE_GREETINGS, CONF_INCLUDE_TIME,
    CONF_TIME_SLOTS, CONF_DND, CONF_PRIORITY, CONF_VOLUME_ENTITY,
    # Import costanti da const.py
    CONF_SERVICE, CONF_SERVICE_DATA, CONF_TARGET, CONF_ENTITY_ID, 
    CONF_ALT_SERVICES, CONF_TYPE, # NUOVE
    # ...
    DEFAULT_NAME, DEFAULT_DATE_FORMAT, DEFAULT_GREETINGS, DEFAULT_INCLUDE_TIME,
    DEFAULT_TIME_SLOTS, DEFAULT_DND, PRIORITY_VOLUME, COMPANION_COMMANDS
)

_LOGGER = logging.getLogger(__name__)

# --- SCHEMI DI VALIDAZIONE ---

# Schema singolo slot temporale
TIME_SLOT_SCHEMA = vol.Schema({
    vol.Required("start"): cv.time,          
    vol.Optional("volume", default=0.5): vol.All(vol.Coerce(float), vol.Range(min=0, max=1))
})

# Schema configurazione slot completa
TIME_SLOTS_CONFIG_SCHEMA = vol.Schema({
    vol.Optional("morning", default=DEFAULT_TIME_SLOTS["morning"]): TIME_SLOT_SCHEMA,
    vol.Optional("afternoon", default=DEFAULT_TIME_SLOTS["afternoon"]): TIME_SLOT_SCHEMA,
    vol.Optional("evening", default=DEFAULT_TIME_SLOTS["evening"]): TIME_SLOT_SCHEMA,
    vol.Optional("night", default=DEFAULT_TIME_SLOTS["night"]): TIME_SLOT_SCHEMA,
})

# Schema DND
DND_SCHEMA = vol.Schema({
    vol.Optional("start", default=DEFAULT_DND["start"]): cv.time,
    vol.Optional("end", default=DEFAULT_DND["end"]): cv.time,
})

# Schema Saluti
GREETINGS_SCHEMA = vol.Schema({
    vol.Optional("morning", default=DEFAULT_GREETINGS["morning"]): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("afternoon", default=DEFAULT_GREETINGS["afternoon"]): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("evening", default=DEFAULT_GREETINGS["evening"]): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("night", default=DEFAULT_GREETINGS["night"]): vol.All(cv.ensure_list, [cv.string]),
})

# Schema per un Servizio Alternativo
ALT_SERVICE_ITEM_SCHEMA = vol.Schema({
    vol.Required(CONF_SERVICE): cv.string,
    vol.Optional(CONF_SERVICE_DATA): dict,
})

# --- SCHEMA CANALI RISOLTO ---
CHANNEL_SCHEMA = vol.Schema({
    vol.Required(CONF_SERVICE): cv.string,
    vol.Optional(CONF_IS_VOICE, default=False): cv.boolean,
    vol.Optional(CONF_ENTITY_ID): cv.entity_ids,           # Target del servizio (es. provider TTS)
    vol.Optional(CONF_VOLUME_ENTITY): cv.entity_ids,       # Target del volume (es. speaker fisico)
    vol.Optional(CONF_TARGET): vol.Any(cv.string, int, list),
    vol.Optional(CONF_SERVICE_DATA): dict,                 # Dati di base del servizio (es. media_player_entity_id)
    # NUOVO: Schema per i servizi alternativi (es. foto/video per Telegram)
    vol.Optional(CONF_ALT_SERVICES): vol.Schema({
        cv.string: ALT_SERVICE_ITEM_SCHEMA
    }),
})

# Schema Configurazione Globale
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_ASSISTANT_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DATE_FORMAT, default=DEFAULT_DATE_FORMAT): cv.string,
        vol.Optional(CONF_INCLUDE_TIME, default=DEFAULT_INCLUDE_TIME): cv.boolean,
        vol.Optional(CONF_GREETINGS, default=GREETINGS_SCHEMA({})): GREETINGS_SCHEMA,
        vol.Optional(CONF_TIME_SLOTS, default=TIME_SLOTS_CONFIG_SCHEMA({})): TIME_SLOTS_CONFIG_SCHEMA,
        vol.Optional(CONF_DND, default=DND_SCHEMA({})): DND_SCHEMA,
        vol.Required(CONF_CHANNELS): vol.Schema({
            cv.string: CHANNEL_SCHEMA
        }),
    }),
}, extra=vol.ALLOW_EXTRA)

# --- HELPER FUNCTIONS ---

def is_time_in_range(start: time, end: time, now: time) -> bool:
    """Verifica se l'orario 'now' è compreso tra start e end, gestendo la mezzanotte."""
    if start <= end:
        return start <= now < end
    else: # Scavalla la mezzanotte
        return start <= now or now < end

def get_current_slot_info(time_slots_conf, now_time):
    """Determina la fascia oraria corrente e il volume."""
    slots = []
    for key, data in time_slots_conf.items():
        slots.append((data["start"], key, data["volume"]))
    
    slots.sort(key=lambda x: x[0])
    
    found_key = None
    found_vol = None
    
    for start, key, vol in slots:
        if now_time >= start:
            found_key = key
            found_vol = vol
        else:
            break
    
    if found_key is None and slots:
        last_slot = slots[-1] 
        found_key = last_slot[1]
        found_vol = last_slot[2]
        
    return found_key, found_vol

# --- MAIN SETUP ---

async def async_setup(hass: HomeAssistant, config: dict):
    """Setup del componente Universal Notifier."""
    if DOMAIN not in config:
        return True
    
    conf = config[DOMAIN]
    channels_config = conf.get(CONF_CHANNELS, {})
    
    base_greetings = conf.get(CONF_GREETINGS) 
    time_slots_conf = conf.get(CONF_TIME_SLOTS)
    dnd_conf = conf.get(CONF_DND)
    
    global_name = conf.get(CONF_ASSISTANT_NAME)
    global_date_fmt = conf.get(CONF_DATE_FORMAT)
    global_include_time = conf.get(CONF_INCLUDE_TIME)

    async def async_send_notification(call: ServiceCall):
        """Servizio unificato di invio."""
        # 1. Parsing Input
        global_raw_message = call.data.get("message", "")
        title = call.data.get("title")
        runtime_data = call.data.get("data", {})
        target_specific_data = call.data.get("target_data", {})
        targets = call.data.get("targets", [])
        
        override_name = call.data.get("assistant_name", global_name)
        skip_greeting = call.data.get("skip_greeting", False)
        include_time = call.data.get(CONF_INCLUDE_TIME, global_include_time)
        is_priority = call.data.get(CONF_PRIORITY, False)
        
        # 2. Contesto Temporale
        now = dt_util.now()
        now_time = now.time()
        
        slot_key, slot_volume = get_current_slot_info(time_slots_conf, now_time)
        is_dnd_active = is_time_in_range(dnd_conf["start"], dnd_conf["end"], now_time)
        
        # 3. Preparazione Saluti e Prefissi
        override_greetings_data = call.data.get(CONF_OVERRIDE_GREETINGS)
        effective_greetings = base_greetings 
        if override_greetings_data:
            effective_greetings = base_greetings.copy() 
            for key, value in override_greetings_data.items():
                if key in effective_greetings:
                    if not isinstance(value, list): value = [value]
                    effective_greetings[key] = value

        options = effective_greetings.get(slot_key, [])
        current_greeting = random.choice(options) if options and not skip_greeting else ""
        
        prefix_parts = [f"[{override_name}"]
        if include_time:
            current_time_str = now.strftime(global_date_fmt)
            prefix_parts.append(f" - {current_time_str}")
        prefix_text = "".join(prefix_parts) + "] "
        
        if isinstance(targets, str):
            targets = [targets]

        tasks = []

        # 4. Iterazione Targets
        for target_alias in targets:
            if target_alias not in channels_config:
                _LOGGER.warning(f"UniNotifier: Target '{target_alias}' sconosciuto.")
                continue

            channel_conf = channels_config[target_alias]
            
            # A. Override Messaggio per Canale e Dati Specifici
            specific_data = {}
            if target_alias in target_specific_data:
                specific_data = target_specific_data[target_alias].copy()
            
            # Sovrascrive il messaggio globale con quello specifico del target
            target_raw_message = specific_data.pop("message", global_raw_message)

            # B. Determinazione del Servizio (Primario o Alternativo)
            
            # I dati specifici (se presenti) hanno la priorità per determinare il tipo
            service_type = specific_data.pop(CONF_TYPE, runtime_data.get(CONF_TYPE, None))
            alt_services_conf = channel_conf.get(CONF_ALT_SERVICES, {})
            
            if service_type and service_type in alt_services_conf:
                # Usa il servizio alternativo
                target_service_conf = alt_services_conf[service_type]
                full_service_name = target_service_conf[CONF_SERVICE]
                base_service_payload = target_service_conf.get(CONF_SERVICE_DATA, {})
                # Forziamo a non voce per i media/dati alternativi (es. Telegram)
                is_voice_channel = False 
            else:
                # Usa il servizio principale
                full_service_name = channel_conf[CONF_SERVICE]
                base_service_payload = channel_conf.get(CONF_SERVICE_DATA, {})
                is_voice_channel = channel_conf[CONF_IS_VOICE]

            # C. Check Comandi (Applicato al messaggio di testo)
            is_command_message = False
            if target_raw_message in COMPANION_COMMANDS or str(target_raw_message).startswith("command_"):
                is_command_message = True

            # D. Costruzione Messaggi
            if is_command_message:
                msg_voice = target_raw_message
                msg_text = target_raw_message
            else:
                msg_voice = f"{current_greeting}. {target_raw_message}" if current_greeting and is_voice_channel else target_raw_message
                msg_text = f"{prefix_text}{msg_voice}" if not is_voice_channel else msg_voice
            
            # E. Logica DND e Volume
            service_entity_id = channel_conf.get(CONF_ENTITY_ID)
            volume_entity_id = channel_conf.get(CONF_VOLUME_ENTITY)
            effective_volume_entity = volume_entity_id if volume_entity_id else service_entity_id
            
            if is_voice_channel:
                if is_dnd_active and not is_priority:
                    _LOGGER.info(f"UniNotifier: Skipped '{target_alias}' (DND attivo)")
                    continue
                
                target_volume = PRIORITY_VOLUME if is_priority else slot_volume
                
                if effective_volume_entity:
                    _LOGGER.debug(f"UniNotifier: Setting volume {target_volume} for {effective_volume_entity}")
                    vol_task = hass.services.async_call(
                        "media_player", 
                        "volume_set", 
                        {"entity_id": effective_volume_entity, "volume_level": target_volume}
                    )
                    tasks.append(vol_task)

            # F. Preparazione Payload Servizio
            try:
                domain, service = full_service_name.split(".", 1)
            except ValueError:
                continue

            if domain not in hass.config.components:
                _LOGGER.warning(f"UniNotifier: Integrazione '{domain}' non caricata.")
                continue

            service_payload = base_service_payload.copy()
            
            # Messaggio nel payload: 'caption' per media, 'message' per altro
            if domain == "telegram_bot" and service_type in ["photo", "video"]:
                service_payload["caption"] = msg_text
            else:
                service_payload["message"] = msg_text
            
            if title: service_payload["title"] = title
            
            # G. Logica di Targetting (FIX PER TTS)
            
            # 1. NOTIFY.*: Usa la chiave 'target' nel payload
            if domain == "notify" and CONF_TARGET in channel_conf:
                service_payload[CONF_TARGET] = channel_conf[CONF_TARGET]
            
            # 2. NON-NOTIFY (es. TTS, Telegram): Se target è configurato, è l'entità del provider.
            elif CONF_TARGET in channel_conf:
                service_payload[CONF_ENTITY_ID] = channel_conf[CONF_TARGET]
            
            # 3. Fallback: Se il canale ha solo 'entity_id' (es. Alexa)
            elif service_entity_id: 
                service_payload[CONF_ENTITY_ID] = service_entity_id

            # H. Gestione e Merge Dati (FIX per NOTIFY DATA)
            
            all_additional_data = {}
            if runtime_data:
                all_additional_data.update(runtime_data)
            
            if specific_data:
                all_additional_data.update(specific_data)
            
            if all_additional_data:
                if domain == "notify":
                    # Per i servizi di notifica (es. mobile_app), i dati aggiuntivi vanno 
                    # sotto la chiave 'data' nel payload principale.
                    if "data" not in service_payload:
                        service_payload["data"] = {}
                        
                    service_payload["data"].update(all_additional_data)
                    
                else:
                    # Per gli altri servizi (tts.speak, telegram_bot.send_message, ecc.), 
                    # i dati accessori si fondono nel payload principale.
                    service_payload.update(all_additional_data)

            tasks.append(hass.services.async_call(domain, service, service_payload))

        if tasks:
            await asyncio.gather(*tasks)

    hass.services.async_register(DOMAIN, "send", async_send_notification)
    return True
