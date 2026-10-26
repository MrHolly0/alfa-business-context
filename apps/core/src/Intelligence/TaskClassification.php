<?php
namespace App\Intelligence;

final class TaskClassification {
    /**
     * Classify company context independently from the classification of its sources.
     * A public feed never makes a private company profile public.
     *
     * @param list<string> $sourceClassifications
     * @return array{data_classification:string,synthetic:bool}
     */
    public static function forCoreTask(bool $hasOrganizerProfile,array $sourceClassifications,bool $explicitSyntheticFixtureTest): array {
        if($hasOrganizerProfile)return ['data_classification'=>'confidential','synthetic'=>false];
        $allSourcesSynthetic=$sourceClassifications!==[] && count(array_filter($sourceClassifications,static fn($value)=>$value!=='synthetic'))===0;
        if($explicitSyntheticFixtureTest && $allSourcesSynthetic)return ['data_classification'=>'synthetic','synthetic'=>true];
        return ['data_classification'=>'confidential','synthetic'=>false];
    }
}
