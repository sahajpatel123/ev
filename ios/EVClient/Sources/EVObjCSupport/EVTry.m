#import "EVTry.h"

static BOOL EVClientFail(NSException *exception, NSError * _Nullable * _Nullable outError) {
    if (outError) {
        NSString *reason = exception.reason ?: @"Objective-C exception";
        *outError = [NSError errorWithDomain:exception.name ?: @"EVClientException"
                                        code:0
                                    userInfo:@{NSLocalizedDescriptionKey: reason}];
    }
    return NO;
}

BOOL EVClientAudioAttachAndPrepare(AVAudioEngine *engine, AVAudioFormat * _Nullable * _Nullable outFormat, NSError * _Nullable * _Nullable outError) {
    @try {
        AVAudioInputNode *node = engine.inputNode;
        [engine prepare];
        if (outFormat) {
            *outFormat = [node inputFormatForBus:0];
        }
        return YES;
    } @catch (NSException *exception) {
        return EVClientFail(exception, outError);
    }
}

BOOL EVClientAudioInstallTap(AVAudioInputNode *node, AVAudioFrameCount bufferSize, AVAudioFormat *format, AVAudioNodeTapBlock block, NSError * _Nullable * _Nullable outError) {
    @try {
        [node installTapOnBus:0 bufferSize:bufferSize format:format block:block];
        return YES;
    } @catch (NSException *exception) {
        return EVClientFail(exception, outError);
    }
}

BOOL EVClientAudioRemoveTap(AVAudioInputNode *node, NSError * _Nullable * _Nullable outError) {
    @try {
        [node removeTapOnBus:0];
        return YES;
    } @catch (NSException *exception) {
        return EVClientFail(exception, outError);
    }
}

BOOL EVClientAudioStartEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError) {
    @try {
        NSError *startError = nil;
        if (![engine startAndReturnError:&startError]) {
            if (outError) {
                *outError = startError;
            }
            return NO;
        }
        return YES;
    } @catch (NSException *exception) {
        return EVClientFail(exception, outError);
    }
}

BOOL EVClientAudioStopEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError) {
    @try {
        if (engine.isRunning) {
            [engine stop];
        }
        return YES;
    } @catch (NSException *exception) {
        return EVClientFail(exception, outError);
    }
}
