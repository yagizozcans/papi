import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import { readStorage, writeStorage } from '@/lib/storage'
import en from './en.json'
import tr from './tr.json'

export const LANG_KEY = 'papi-lang'
export type Lang = 'tr' | 'en'

function initialLang(): Lang {
  return readStorage(LANG_KEY) === 'en' ? 'en' : 'tr'
}

void i18n.use(initReactI18next).init({
  resources: { tr: { translation: tr }, en: { translation: en } },
  lng: initialLang(),
  fallbackLng: 'tr',
  interpolation: { escapeValue: false },
  returnNull: false,
})

i18n.on('languageChanged', (lng) => {
  writeStorage(LANG_KEY, lng)
  document.documentElement.lang = lng
})
document.documentElement.lang = i18n.language

export function toggleLanguage(): void {
  void i18n.changeLanguage(i18n.language === 'tr' ? 'en' : 'tr')
}

export default i18n
