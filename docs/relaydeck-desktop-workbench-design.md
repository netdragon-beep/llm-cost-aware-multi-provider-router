# RelayDeck Desktop Workbench - Stage 1

## Goal

Keep the current local gateway and single-page admin panel. Improve the operator experience by making supplier configuration and model routes visibly connected, and by adding a Chinese-first fixed-theme system.

## Supplier-route relationship panel

The existing supplier/API and public-model route editors remain independent. A compact relationship panel sits within the routing workspace and reflects the active selection.

- Selecting a supplier or API profile lists its published public-model bindings.
- Selecting a public model lists the upstream supplier/API bindings, priority, and fallback order.
- Each relationship item can focus the related supplier/API or public-model card.
- The panel displays health, quota availability, and current route status as context only. It does not edit upstream credentials or alter routing behavior.
- Empty states explain whether the current object has no bindings or no enabled published bindings.

## Appearance settings

System Settings gains an Appearance section. Theme selection is local to the browser and has no effect on gateway configuration.

Fixed presets:

- Morning: neutral light with blue emphasis.
- Mist: low-contrast light gray.
- Deep Space: dark blue-gray.
- Ink: high-contrast dark.
- Forest: green emphasis.
- Amber: warm emphasis.

Themes affect page backgrounds, text, borders, panel surfaces, focus states, and the application accent. Semantic success, warning, and error colors remain stable and are never reinterpreted by the selected theme.

## Implementation boundaries

- Extend `admin-panel/static/index.html` only for Stage 1 UI, client state, and browser-local persistence.
- Reuse the current supplier, API profile, model route, route binding, status, and quota data already returned by the admin API.
- Do not change LiteLLM configuration generation or running gateway behavior.
- Preserve existing deep links, modals, and responsive layout.

## Verification

- Exercise both supplier-first and model-first selection paths.
- Confirm relationship items locate their related cards.
- Confirm every theme persists after a page reload.
- Confirm semantic health and failure colors remain distinguishable in light and dark themes.
