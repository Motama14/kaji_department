import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', loadComponent: () => import('./viewer/viewer.component').then((m) => m.ViewerComponent), title: 'Hotspot Atlas' },
  { path: 'calendar', loadComponent: () => import('./calendar/calendar').then((m) => m.CalendarComponent), title: 'Burn calendar · Hotspot Atlas' },
  { path: '**', redirectTo: '' },
];
