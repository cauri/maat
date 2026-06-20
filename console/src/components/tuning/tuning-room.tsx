"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { ConfigKnobs } from "./config-knobs";
import { PromptsEditor } from "./prompts-editor";

export function TuningRoom() {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 sm:p-6">
      <Tabs defaultValue="config">
        <TabsList>
          <TabsTrigger value="config">Config</TabsTrigger>
          <TabsTrigger value="prompts">Prompts</TabsTrigger>
        </TabsList>
        <TabsContent value="config" className="mt-4">
          <p className="mb-3 text-sm text-muted-foreground">
            Scoring settings. Propose stages a value to review; Promote makes it live — which needs
            your sign-off.
          </p>
          <ConfigKnobs />
        </TabsContent>
        <TabsContent value="prompts" className="mt-4">
          <p className="mb-3 text-sm text-muted-foreground">
            The live instructions each agent runs. Saving an edit makes it live and needs your
            sign-off.
          </p>
          <PromptsEditor />
        </TabsContent>
      </Tabs>
    </div>
  );
}
