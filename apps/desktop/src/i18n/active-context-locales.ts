import type { TranslationOverrides } from './define-locale'

export const deActiveContext = {
  unknownProfile: "unbekanntes Profil",
  unknownTenant: "unbekannter Mandant",
  unknownDevice: "unbekanntes Gerät",
  newChat: "Neuer Chat",
  chat: "Chat",
  thisChat: "Dieser Chat",
  ownerUnknown: (profile, device) => `Besitzer unbekannt — ${profile} auf ${device}`,
  ownerKnown: (profile, device) => `${profile} auf ${device}`,
  detail: (what, owner, tenant) => `${what}: ${owner}; Mandant ${tenant}`,
  text: (what, profile, tenant, device) => `${what} · ${profile} · ${tenant} · ${device}`
} satisfies TranslationOverrides['activeContext']

export const esActiveContext = {
  unknownProfile: "perfil desconocido",
  unknownTenant: "inquilino desconocido",
  unknownDevice: "dispositivo desconocido",
  newChat: "Nuevo chat",
  chat: "Chat",
  thisChat: "Este chat",
  ownerUnknown: (profile, device) => `propietario desconocido — ${profile} en ${device}`,
  ownerKnown: (profile, device) => `${profile} en ${device}`,
  detail: (what, owner, tenant) => `${what}: ${owner}; inquilino ${tenant}`,
  text: (what, profile, tenant, device) => `${what} · ${profile} · ${tenant} · ${device}`
} satisfies TranslationOverrides['activeContext']

export const frActiveContext = {
  unknownProfile: "profil inconnu",
  unknownTenant: "espace inconnu",
  unknownDevice: "appareil inconnu",
  newChat: "Nouvelle discussion",
  chat: "Discussion",
  thisChat: "Cette discussion",
  ownerUnknown: (profile, device) => `propriétaire inconnu — ${profile} sur ${device}`,
  ownerKnown: (profile, device) => `${profile} sur ${device}`,
  detail: (what, owner, tenant) => `${what} : ${owner} ; espace ${tenant}`,
  text: (what, profile, tenant, device) => `${what} · ${profile} · ${tenant} · ${device}`
} satisfies TranslationOverrides['activeContext']

export const ruActiveContext = {
  unknownProfile: "неизвестный профиль",
  unknownTenant: "неизвестный арендатор",
  unknownDevice: "неизвестное устройство",
  newChat: "Новый чат",
  chat: "Чат",
  thisChat: "Этот чат",
  ownerUnknown: (profile, device) => `владелец неизвестен — ${profile} на ${device}`,
  ownerKnown: (profile, device) => `${profile} на ${device}`,
  detail: (what, owner, tenant) => `${what}: ${owner}; арендатор ${tenant}`,
  text: (what, profile, tenant, device) => `${what} · ${profile} · ${tenant} · ${device}`
} satisfies TranslationOverrides['activeContext']
