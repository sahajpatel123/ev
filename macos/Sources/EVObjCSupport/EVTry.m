#import "EVTry.h"

static BOOL EVFail(NSException *exception, NSError * _Nullable * _Nullable outError) {
    if (outError) {
        NSString *reason = exception.reason ?: @"Objective-C exception";
        *outError = [NSError errorWithDomain:exception.name ?: @"EVException"
                                        code:0
                                    userInfo:@{NSLocalizedDescriptionKey: reason}];
    }
    return NO;
}

BOOL EVAudioAttachAndPrepare(AVAudioEngine *engine, AVAudioFormat * _Nullable * _Nullable outFormat, NSError * _Nullable * _Nullable outError) {
    @try {
        AVAudioInputNode *node = engine.inputNode;
        [engine prepare];
        if (outFormat) {
            *outFormat = [node inputFormatForBus:0];
        }
        return YES;
    } @catch (NSException *exception) {
        return EVFail(exception, outError);
    }
}

BOOL EVAudioInstallTap(AVAudioInputNode *node, AVAudioFrameCount bufferSize, AVAudioFormat *format, AVAudioNodeTapBlock block, NSError * _Nullable * _Nullable outError) {
    @try {
        [node installTapOnBus:0 bufferSize:bufferSize format:format block:block];
        return YES;
    } @catch (NSException *exception) {
        return EVFail(exception, outError);
    }
}

BOOL EVAudioRemoveTap(AVAudioInputNode *node, NSError * _Nullable * _Nullable outError) {
    @try {
        [node removeTapOnBus:0];
        return YES;
    } @catch (NSException *exception) {
        return EVFail(exception, outError);
    }
}

BOOL EVAudioStartEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError) {
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
        return EVFail(exception, outError);
    }
}

BOOL EVAudioStopEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError) {
    @try {
        if (engine.isRunning) {
            [engine stop];
        }
        return YES;
    } @catch (NSException *exception) {
        return EVFail(exception, outError);
    }
}

UNUserNotificationCenter * _Nullable EVNotificationCenterOrNil(void) {
    // currentNotificationCenter aborts inside dispatch_once when the
    // process is not an .app bundle (`bundleProxyForCurrentProcess is nil`).
    // That exception is not catchable. Skip the call for CLI / SPM binaries.
    NSBundle *bundle = [NSBundle mainBundle];
    NSString *path = bundle.bundlePath ?: @"";
    if (path.length == 0 || ![[path pathExtension] isEqualToString:@"app"]) {
        return nil;
    }
    if (bundle.bundleIdentifier.length == 0) {
        return nil;
    }
    @try {
        return [UNUserNotificationCenter currentNotificationCenter];
    } @catch (NSException *exception) {
        return nil;
    }
}

BOOL EVRaiseAndCatchForTests(NSError * _Nullable * _Nullable outError) {
    @try {
        [NSException raise:@"EVTest" format:@"guard-me"];
        return YES;
    } @catch (NSException *exception) {
        return EVFail(exception, outError);
    }
}
