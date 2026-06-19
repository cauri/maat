"use client";

import { LogOut, ShieldAlert, UserRound } from "lucide-react";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { useShell } from "./shell-context";

function initials(email: string): string {
  const name = email.split("@")[0] ?? email;
  const parts = name.split(/[.\-_]+/).filter(Boolean);
  const letters = (parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "");
  return (letters || name.slice(0, 2) || "?").toUpperCase();
}

export function UserMenu() {
  const { user, authEnabled, logoutPath } = useShell();
  const email = user?.email ?? "";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Account">
          <Avatar className="size-6">
            <AvatarFallback className="text-[0.625rem]">
              {email ? initials(email) : <UserRound className="size-3.5" />}
            </AvatarFallback>
          </Avatar>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuLabel className="flex flex-col gap-0.5">
          <span className="text-xs font-normal text-muted-foreground">Signed in as</span>
          <span className="truncate font-medium">{email || "Local operator"}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {authEnabled ? (
          <DropdownMenuItem asChild variant="destructive">
            <a href={logoutPath}>
              <LogOut /> Sign out
            </a>
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem disabled>
            <ShieldAlert /> Admin gate disabled (dev)
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
