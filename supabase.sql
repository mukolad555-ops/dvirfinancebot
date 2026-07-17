create table if not exists public.cash_operations (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    operation_date date not null default ((now() at time zone 'Europe/Kyiv')::date),
    operation_type text not null check (
        operation_type in ('income', 'expense', 'exchange_in', 'exchange_out')
    ),
    currency text not null check (currency in ('UAH', 'USD', 'EUR')),
    amount numeric(18,2) not null check (amount > 0),
    description text,
    telegram_user_id bigint,
    telegram_username text,
    telegram_full_name text,
    telegram_chat_id bigint not null
);

create index if not exists cash_operations_chat_date_idx
on public.cash_operations (telegram_chat_id, operation_date);

alter table public.cash_operations enable row level security;

drop policy if exists "service role full access" on public.cash_operations;
create policy "service role full access"
on public.cash_operations
for all
to service_role
using (true)
with check (true);
