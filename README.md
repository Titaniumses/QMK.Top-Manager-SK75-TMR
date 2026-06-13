# QMK.Top Manager

**Автоматическое переключение профилей и мониторинг батареи для клавиатур qmk.top**

**Automatic profile switching & battery monitoring for qmk.top keyboards**

The app now runs offline by default. Network access is only available if you
explicitly enable the sniffer or a future update-check hook.

---

## 🇷🇺 Русский

### Что это?

QMK.Top Manager — настольное приложение для Windows, которое автоматически переключает профили вашей клавиатуры в зависимости от того, какая программа сейчас активна. Играете в CS2 — активируется игровой профиль. Переключились в браузер — возвращается обычный. Всё происходит мгновенно и без вашего участия.

### Возможности

- **Автоматическое переключение профилей** — привяжите профиль к любой программе (игре, редактору, браузеру), и клавиатура сама переключится когда вы в неё перейдёте
- **Ручное переключение горячими клавишами** — назначьте глобальные сочетания клавиш для мгновенного переключения
- **Мониторинг батареи** — для беспроводных клавиатур отображается уровень заряда в трее и в окне приложения (обновляется каждую минуту)
- **Управление частотой опроса** — переключайте polling rate (125–8000 Гц) вместе с профилем (только магнитные клавиатуры)
- **Управление подсветкой** — переключайте профиль подсветки вместе с основным профилем (только магнитные клавиатуры)
- **Работа в трее** — приложение сворачивается в системный трей и не мешает работе; иконка показывает уровень батареи
- **Автозапуск с Windows** — включается одной галочкой в настройках
- **Уведомления** — Windows-уведомления при переключении профиля

### Поддерживаемые клавиатуры

Приложение работает с клавиатурами, которые настраиваются через сайт **qmk.top**. Это клавиатуры с Vendor ID `0x3151`. Поддерживаются два типа:

| Тип | Профилей | Polling rate | Подсветка |
|-----|----------|-------------|-----------|
| Магнитные (magnetic) | 4 | Да (125–8000 Гц) | Да |
| Механические (mechanical) | 3 | Нет | Нет |

Подключение: USB-кабель или 2.4G беспроводной донгл.

### Как пользоваться

1. **Запустите приложение** и подключите клавиатуру (USB или донгл)
2. **Выберите тип клавиатуры** — при первом подключении появится мастер настройки. Выберите «Магнитная» или «Механическая» (это важно — неправильный выбор может нарушить работу)
3. **Настройте профили** — дайте имена, назначьте горячие клавиши, выберите частоту опроса и подсветку
4. **Добавьте привязки к программам** — укажите имя процесса (например `cs2.exe`) и целевой профиль
5. **Нажмите «Старт»** — сервис начнёт работу, профили будут переключаться автоматически
6. **Закройте окно** — приложение уйдёт в трей и продолжит работать

### Требования

- Windows 10/11
- Клавиатура qmk.top, подключённая по USB или через 2.4G донгл
- Для мониторинга батареи беспроводной клавиатуры: начальная настройка через встроенный сниффер (один раз)

---

## 🇬🇧 English

### What is this?

QMK.Top Manager is a Windows desktop app that automatically switches your keyboard profiles based on the active application. Playing CS2 — your gaming profile activates. Switching to a browser — it goes back to default. Instantly and hands-free.

### Features

- **Automatic profile switching** — bind a profile to any app (game, editor, browser), and the keyboard switches when you focus that window
- **Manual switching via hotkeys** — assign global shortcuts for instant profile changes
- **Battery monitoring** — for wireless keyboards, battery level is shown in the system tray and app window (updates every minute)
- **Polling rate control** — switch polling rate (125–8000 Hz) together with profiles (magnetic keyboards only)
- **Lighting control** — switch lighting profiles along with keyboard profiles (magnetic keyboards only)
- **System tray** — the app minimizes to tray and stays out of the way; the tray icon shows battery level
- **Windows autostart** — enable with a single checkbox
- **Notifications** — Windows toast notifications on profile switch

### Supported keyboards

The app works with keyboards configured through **qmk.top**. These are keyboards with Vendor ID `0x3151`. Two types are supported:

| Type | Profiles | Polling rate | Lighting |
|------|----------|-------------|----------|
| Magnetic | 4 | Yes (125–8000 Hz) | Yes |
| Mechanical | 3 | No | No |

Connection: USB cable or 2.4G wireless dongle.

### How to use

1. **Launch the app** and connect your keyboard (USB or dongle)
2. **Select keyboard type** — a setup wizard appears on first connection. Choose "Magnetic" or "Mechanical" (important — wrong choice may corrupt settings)
3. **Configure profiles** — set names, assign hotkeys, choose polling rate and lighting
4. **Add process bindings** — specify a process name (e.g. `cs2.exe`) and target profile
5. **Click "Start"** — the service begins, profiles switch automatically
6. **Close the window** — the app goes to tray and keeps running

### Requirements

- Windows 10/11
- A qmk.top keyboard connected via USB or 2.4G dongle
- For wireless battery monitoring: one-time setup via the built-in sniffer

---

## Screenshots

*Coming soon*

## License

MIT
