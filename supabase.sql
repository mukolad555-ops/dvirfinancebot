create table if not exists public.cash_operations (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    operation_date date not null default ((now() at time zone 'Europe/Kyiv')::date),
    operation_type text not null,
    currency text not null,
    amount numeric(18,2) not null check (amount > 0),
    description text,
    telegram_user_id bigint,
    telegram_username text,
    telegram_full_name text,
    telegram_chat_id bigint not null
);

alter table public.cash_operations
    add column if not exists telegram_user_id bigint,
    add column if not exists telegram_username text,
    add column if not exists telegram_full_name text,
    add column if not exists telegram_chat_id bigint;

alter table public.cash_operations drop constraint if exists cash_operations_operation_type_check;
alter table public.cash_operations add constraint cash_operations_operation_type_check
check (operation_type in ('income','expense','exchange_in','exchange_out'));

alter table public.cash_operations drop constraint if exists cash_operations_currency_check;
alter table public.cash_operations add constraint cash_operations_currency_check
check (currency in ('UAH','USD','EUR'));

create index if not exists cash_operations_chat_date_idx
on public.cash_operations (telegram_chat_id, operation_date);

alter table public.cash_operations enable row level security;
drop policy if exists "service role full access" on public.cash_operations;
create policy "service role full access" on public.cash_operations
for all to service_role using (true) with check (true);
